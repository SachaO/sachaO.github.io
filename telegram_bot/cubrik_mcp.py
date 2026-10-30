"""Read-only ATLAS client for the official Cubrik MCP server.

This module can only invoke an explicit allowlist of Cubrik read tools.
It never invokes agent-management, order, trading, or broker-write tools.
OAuth tokens and client registration data are encrypted in the existing
PostgreSQL database using a Fernet key held in Render environment variables.
"""
import asyncio
import json
import logging
import os
import queue
import threading
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx2
import psycopg
from cryptography.fernet import Fernet, InvalidToken
from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.auth.exceptions import OAuthRegistrationError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl


log = logging.getLogger(__name__)

DEFAULT_SERVER_URL = "https://mcp.cubrik.ai/mcp"
DEFAULT_CALLBACK_URI = "https://sachao-github-io.onrender.com/cubrik/callback"

_PENDING_LOCK = threading.RLock()
_PENDING = None
_ACTIVE_WORKER = None

# These are the only tool names ATLAS is allowed to call. No trading or
# agent mutation tool is ever selected, even if Cubrik exposes it.
READ_TOOL_ALIASES = {
    "account": ("get_account", "account_overview", "get_cubrik_account"),
    "platforms": ("list_platforms", "get_platforms"),
    "portfolio": ("get_portfolio", "get_account_portfolio"),
    "agents": ("list_agents", "get_agents", "list_tasks"),
    "notifications": ("list_notifications", "get_notifications"),
    "results": ("get_trading_results", "get_results"),
    "history": ("get_trade_history", "list_trade_history"),
    "agent_runs": ("get_agent_runs", "list_agent_runs"),
}


class CubrikMonitorError(RuntimeError):
    pass


def _server_url():
    return os.getenv("CUBRIK_MCP_SERVER_URL", DEFAULT_SERVER_URL).strip().rstrip("/")


def _redirect_uri():
    explicit = os.getenv("CUBRIK_MCP_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    public = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if public:
        return public + "/cubrik/callback"
    return DEFAULT_CALLBACK_URI


def _fernet():
    key = os.getenv("CUBRIK_MCP_ENCRYPTION_KEY", "").strip()
    if not key:
        raise CubrikMonitorError(
            "La clé de chiffrement CUBRIK_MCP_ENCRYPTION_KEY manque dans Render."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CubrikMonitorError("La clé de chiffrement Cubrik de Render est invalide.") from exc


def _db_url():
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise CubrikMonitorError(
            "DATABASE_URL manque dans Render : ATLAS ne peut pas conserver la connexion OAuth."
        )
    return value


def _read_secret(slot):
    cipher = _fernet()
    with psycopg.connect(_db_url(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS atlas_cubrik_mcp_oauth (
                    slot TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "SELECT payload FROM atlas_cubrik_mcp_oauth WHERE slot = %s",
                (slot,),
            )
            row = cur.fetchone()
    if not row:
        return None
    try:
        return cipher.decrypt(row[0].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise CubrikMonitorError(
            "Les identifiants OAuth Cubrik enregistrés ne peuvent pas être déchiffrés. Vérifie la clé de chiffrement Render."
        ) from exc


def _write_secret(slot, payload):
    cipher = _fernet()
    encrypted = cipher.encrypt(payload.encode("utf-8")).decode("ascii")
    with psycopg.connect(_db_url(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS atlas_cubrik_mcp_oauth (
                    slot TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                INSERT INTO atlas_cubrik_mcp_oauth(slot, payload, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT(slot)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                """,
                (slot, encrypted),
            )


class PostgresOAuthStorage:
    """MCP OAuth TokenStorage with encrypted PostgreSQL persistence."""

    async def get_tokens(self):
        payload = await asyncio.to_thread(_read_secret, "tokens")
        return OAuthToken.model_validate_json(payload) if payload else None

    async def set_tokens(self, tokens):
        await asyncio.to_thread(_write_secret, "tokens", tokens.model_dump_json())

    async def get_client_info(self):
        payload = await asyncio.to_thread(_read_secret, "client_info")
        return OAuthClientInformationFull.model_validate_json(payload) if payload else None

    async def set_client_info(self, client_info):
        await asyncio.to_thread(_write_secret, "client_info", client_info.model_dump_json())


def is_connected():
    """True only when an encrypted access/refresh-token record is present."""
    try:
        return _read_secret("tokens") is not None
    except Exception:
        log.exception("Unable to inspect stored Cubrik MCP credentials")
        return False


def _jsonable_tool_result(result):
    if getattr(result, "isError", False):
        raise CubrikMonitorError("Cubrik a refusé l'appel d'un outil de lecture.")
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    texts = []
    for item in getattr(result, "content", []) or []:
        value = getattr(item, "text", None)
        if value:
            texts.append(value)
    joined = "\n".join(texts).strip()
    if not joined:
        return {}
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return {"text": joined}


def _normal_name(name):
    value = str(name or "").lower()
    for prefix in ("mcp__cubrik__", "cubrik_", "cubrik__"):
        value = value.replace(prefix, "")
    return "".join(ch for ch in value if ch.isalnum())


def _find_tool(tools, aliases):
    by_normal = {_normal_name(tool.name): tool.name for tool in tools}
    for alias in aliases:
        actual = by_normal.get(_normal_name(alias))
        if actual:
            return actual
    return None


async def _call_read_tool(session, tools, logical_name, arguments=None):
    aliases = READ_TOOL_ALIASES[logical_name]
    actual_name = _find_tool(tools, aliases)
    if not actual_name:
        return None, "Outil de lecture « {} » indisponible avec les autorisations Cubrik actuelles.".format(logical_name)
    try:
        response = await session.call_tool(actual_name, arguments=arguments or {})
        return _jsonable_tool_result(response), None
    except Exception as exc:
        # Do not include arguments, access tokens or raw request headers in logs.
        log.warning("Cubrik read tool %s failed: %s", logical_name, type(exc).__name__)
        return None, "La lecture « {} » a échoué ({}).".format(logical_name, type(exc).__name__)


def _items(payload, keys):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for value in payload.values():
            if isinstance(value, dict):
                for key in keys:
                    nested = value.get(key)
                    if isinstance(nested, list):
                        return nested
    return []


def _money(value):
    try:
        return "{:,.2f}".format(float(value)).replace(",", " ")
    except (TypeError, ValueError):
        return "non communiqué"


def _schedule(agent):
    trigger = agent.get("trigger") or agent.get("schedule") or {}
    kind = trigger.get("kind") or trigger.get("type") or ""
    if kind == "every":
        return "toutes les {} min".format(trigger.get("minutes", "?"))
    if kind == "hourly":
        return "toutes les heures"
    if kind == "daily":
        return "chaque jour à {:02d}h{:02d}".format(
            int(trigger.get("hour", 0)), int(trigger.get("minute", 0))
        )
    if kind == "weekly":
        return "chaque semaine"
    return str(kind or "horaire non communiqué")


def _short_value(value, limit=180):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value).replace("\n", " ").strip()[:limit]


def _pick(data, names):
    if not isinstance(data, dict):
        return None
    for name in names:
        value = data.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _format_results(payload):
    if not isinstance(payload, dict):
        return _short_value(payload, 900)
    fields = [
        ("P&L net", ("pnl", "profit_loss", "net_pnl", "total_pnl")),
        ("P&L réalisé", ("realized_pnl", "realizedPnl")),
        ("P&L latent", ("unrealized_pnl", "unrealizedPnl")),
        ("Nombre de trades", ("trade_count", "trades_count", "total_trades")),
        ("Taux de réussite", ("win_rate", "winRate")),
        ("Perte maximale", ("max_drawdown", "maxDrawdown")),
    ]
    lines = []
    for label, keys in fields:
        value = _pick(payload, keys)
        if value is not None:
            rendered = _money(value) if isinstance(value, (int, float)) else _short_value(value, 100)
            if "taux" in label.lower() and isinstance(value, (int, float)) and value <= 1:
                rendered = "{:.1f} %".format(float(value) * 100)
            elif "taux" in label.lower() and isinstance(value, (int, float)):
                rendered = "{:.1f} %".format(float(value))
            lines.append("• {} : {}".format(label, rendered))
    if not lines:
        summary = _pick(payload, ("summary", "message", "text", "description"))
        if summary:
            lines.append(_short_value(summary, 900))
        else:
            lines.append(_short_value(payload, 900))
    return "\n".join(lines)


def _format_agent_runs(value):
    runs = _items(value, ("runs", "items", "history", "data"))
    if not runs and isinstance(value, dict):
        runs = [value]
    if not runs:
        return _short_value(value, 320)
    lines = []
    for run in runs[:3]:
        if not isinstance(run, dict):
            lines.append(_short_value(run, 240))
            continue
        at = _pick(run, ("at", "created_at", "started_at", "startedAt", "timestamp", "time"))
        status = _pick(run, ("status", "result_status", "outcome"))
        summary = _pick(run, ("summary", "decision", "message", "result", "explanation", "response"))
        parts = []
        if at:
            parts.append(str(at))
        if status:
            parts.append(str(status))
        if summary:
            parts.append(_short_value(summary, 260))
        if not parts:
            parts.append(_short_value(run, 260))
        lines.append(" · ".join(parts))
    return "\n".join(lines)


def _format_trade_history(payload):
    trades = _items(payload, ("trades", "history", "items", "data", "fills"))
    if not trades:
        if isinstance(payload, dict):
            summary = _pick(payload, ("summary", "message", "text"))
            if summary:
                return _short_value(summary, 900)
        return "Aucune transaction récente renvoyée par Cubrik."
    lines = []
    for trade in trades[:10]:
        if not isinstance(trade, dict):
            lines.append(_short_value(trade, 180))
            continue
        at = _pick(trade, ("at", "created_at", "timestamp", "time", "closed_at"))
        instrument = _pick(trade, ("instrument", "symbol", "instId", "market")) or "Actif inconnu"
        side = _pick(trade, ("side", "direction", "action")) or "opération"
        size = _pick(trade, ("size", "quantity", "qty", "fillSz"))
        price = _pick(trade, ("price", "avg_price", "fillPx", "entry_price"))
        pnl = _pick(trade, ("pnl", "profit_loss", "realized_pnl", "realizedPnl"))
        parts = [str(instrument), str(side)]
        if size is not None:
            parts.append("quantité " + _short_value(size, 40))
        if price is not None:
            parts.append("prix " + _short_value(price, 40))
        if pnl is not None:
            parts.append("P&L " + _short_value(pnl, 40))
        if at:
            parts.append("le " + str(at))
        lines.append("• " + " · ".join(parts))
    return "\n".join(lines)


def _portfolio_accounts(payload):
    return _items(payload, ("accounts", "platforms", "items", "data"))


def _account_data(payload):
    if not isinstance(payload, dict):
        return {}
    return payload.get("account") if isinstance(payload.get("account"), dict) else payload


def _extract_agents(payload):
    return _items(payload, ("tasks", "agents", "items", "data"))


def _extract_platforms(payload):
    return _items(payload, ("platforms", "accounts", "items", "data"))


def _extract_positions(payload):
    if isinstance(payload, dict):
        for key in ("accounts", "platforms"):
            entries = payload.get(key)
            if isinstance(entries, list):
                all_positions = []
                for entry in entries:
                    if isinstance(entry, dict):
                        all_positions.extend(entry.get("positions") or [])
                if all_positions:
                    return all_positions
        return payload.get("positions") or []
    return []


def _tool_has_name(tools, logical_name):
    return _find_tool(tools, READ_TOOL_ALIASES[logical_name]) is not None


async def _read_cubrik_snapshot(auth_url_callback=None, callback_result=None, detailed=True):
    server_url = _server_url()
    redirect_uri = _redirect_uri()
    storage = PostgresOAuthStorage()

    async def redirect_handler(auth_url):
        if auth_url_callback:
            await auth_url_callback(auth_url)
        else:
            raise CubrikMonitorError(
                "La session Cubrik doit être reconnectée. Utilise /cubrikconnect."
            )

    async def oauth_callback_handler():
        if callback_result is None:
            raise CubrikMonitorError(
                "Autorisation Cubrik nécessaire. Utilise /cubrikconnect."
            )
        response = await asyncio.to_thread(callback_result.get, True, 600)
        if response.get("error"):
            raise CubrikMonitorError("Autorisation refusée : {}".format(response["error"][:140]))
        return AuthorizationCodeResult(
            code=response.get("code", ""),
            state=response.get("state"),
            iss=response.get("iss"),
        )

    oauth = OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name="ATLAS Telegram Monitor",
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            application_type="web",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=oauth_callback_handler,
    )

    async with httpx2.AsyncClient(auth=oauth, follow_redirects=True, timeout=httpx2.Timeout(30.0, read=300.0)) as http_client:
        async with streamable_http_client(server_url, http_client=http_client) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tool_result = await session.list_tools()
                tools = tool_result.tools or []
                snapshot = {"captured_at": datetime.now().astimezone().strftime("%d/%m/%Y %H:%M")}
                warnings = []

                # Read-only calls only. No create/update/start/pause/trade tool is selected.
                jobs = [
                    ("account", {}),
                    ("platforms", {}),
                    ("portfolio", {"refresh": True}),
                    ("agents", {}),
                    ("notifications", {"limit": 8}),
                    ("results", {"period": "past_week"}),
                ]
                responses = {}
                for logical_name, arguments in jobs:
                    value, warning = await _call_read_tool(
                        session, tools, logical_name, arguments
                    )
                    responses[logical_name] = value
                    if warning:
                        warnings.append(warning)

                agents = _extract_agents(responses.get("agents"))
                if detailed and _tool_has_name(tools, "agent_runs"):
                    recent_runs = []
                    for agent in agents[:5]:
                        agent_id = agent.get("id") or agent.get("agent_id")
                        if not agent_id:
                            continue
                        value, warning = await _call_read_tool(
                            session,
                            tools,
                            "agent_runs",
                            {"agent_id": str(agent_id), "limit": 3},
                        )
                        if value is not None:
                            recent_runs.append({
                                "agent": agent.get("name") or agent.get("title") or str(agent_id),
                                "runs": value,
                            })
                        if warning:
                            warnings.append(warning)
                    responses["agent_runs"] = recent_runs

                if detailed:
                    value, warning = await _call_read_tool(
                        session, tools, "history", {"days": 7, "limit": 10}
                    )
                    responses["history"] = value
                    if warning:
                        warnings.append(warning)

                snapshot.update(responses)
                snapshot["warnings"] = warnings
                snapshot["available_tools"] = [tool.name for tool in tools]
                return snapshot


def format_cubrik_snapshot(data, detailed=True):
    account = _account_data(data.get("account"))
    credits = account.get("credits") or {}
    agents_summary = account.get("agents") or {}
    platforms = _extract_platforms(data.get("platforms"))
    agents = _extract_agents(data.get("agents"))
    positions = _extract_positions(data.get("portfolio"))
    notifications = _items(data.get("notifications"), ("notifications", "items", "data"))

    lines = [
        "🤖 ATLAS — ÉTAT DE CUBRIK",
        "Relevé : {}".format(data.get("captured_at", "heure inconnue")),
    ]

    plan = account.get("plan") or account.get("subscription") or "non communiqué"
    lines.extend([
        "",
        "📦 ABONNEMENT ET CRÉDITS",
        "Forfait : {}".format(plan),
        "Agents actifs : {} / {}".format(
            agents_summary.get("active", sum(1 for a in agents if str(a.get("status", "")).lower() in ("active", "running"))),
            agents_summary.get("limit", "?"),
        ),
        "Crédits restants : {} / {}".format(
            credits.get("left", "?"), credits.get("month", "?")
        ),
    ])
    if credits.get("renews"):
        lines.append("Renouvellement des crédits : {}".format(credits["renews"]))

    lines.extend(["", "🔌 PLATEFORMES CONNECTÉES"])
    if not platforms:
        lines.append("Aucune plateforme renvoyée par Cubrik.")
    else:
        for platform in platforms[:8]:
            lines.append("• {} — {} — solde {}".format(
                platform.get("name") or platform.get("id") or "Plateforme",
                platform.get("status") or ("active" if platform.get("agentsCanUse") else "état inconnu"),
                _money(platform.get("balance")) + (" " + str(platform.get("currency")) if platform.get("currency") else ""),
            ))

    lines.extend(["", "🧠 AGENTS CUBRIK ({})".format(len(agents))])
    if not agents:
        lines.append("Aucun agent renvoyé, ou accès aux agents non autorisé.")
    else:
        for agent in agents[:8]:
            name = agent.get("name") or agent.get("title") or "Agent sans nom"
            status = agent.get("status") or "état inconnu"
            schedule = _schedule(agent)
            last_run = agent.get("lastRunAt") or agent.get("last_run_at") or agent.get("lastScheduledRunAt")
            line = "• {} — {} — {}".format(name, status, schedule)
            if last_run:
                line += " — dernier passage : " + str(last_run)
            if agent.get("mayTrade") is not None:
                line += " — trading : " + ("autorisé" if agent.get("mayTrade") else "interdit")
            lines.append(line)

    portfolio_accounts = _portfolio_accounts(data.get("portfolio"))
    if portfolio_accounts:
        lines.extend(["", "💼 COMPTES BROKER VUS PAR CUBRIK"])
        for account in portfolio_accounts[:6]:
            name = account.get("name") or account.get("platform") or "Compte"
            equity = account.get("equity")
            balance = account.get("balance")
            amount = equity if equity is not None else balance
            if amount is not None:
                lines.append("• {} : {}".format(name, _money(amount)))
            else:
                lines.append("• {} : solde non communiqué".format(name))

    lines.extend(["", "📈 POSITIONS OUVERTES ({})".format(len(positions))])
    if not positions:
        lines.append("Aucune position ouverte renvoyée par Cubrik.")
    else:
        for position in positions[:10]:
            lines.append("• {} — taille {} — P&L latent {} {}".format(
                position.get("instrument") or position.get("instId") or position.get("symbol") or "?",
                position.get("size") or position.get("pos") or "?",
                _money(position.get("unrealized_pnl") or position.get("upl")),
                position.get("currency") or "USD",
            ))

    if detailed:
        results = data.get("results")
        if results is not None:
            lines.extend(["", "📊 RÉSULTATS (7 derniers jours)", _format_results(results)])

        history = data.get("history")
        if history is not None:
            lines.extend(["", "🧾 HISTORIQUE DES TRANSACTIONS", _format_trade_history(history)])

        run_groups = data.get("agent_runs") or []
        if run_groups:
            lines.extend(["", "⏱️ DERNIÈRES EXÉCUTIONS DES AGENTS"])
            for group in run_groups[:5]:
                lines.append("• {} :\n  {}".format(group["agent"], _format_agent_runs(group["runs"])))

        if notifications:
            lines.extend(["", "🔔 DERNIÈRES NOTIFICATIONS"])
            for notification in notifications[:5]:
                lines.append("• " + _short_value(
                    notification.get("message") or notification.get("title") or notification, 240
                ))

    if data.get("warnings"):
        lines.extend(["", "⚠️ INFORMATIONS INCOMPLÈTES"])
        lines.extend("• " + warning for warning in data["warnings"][:6])

    lines.extend([
        "",
        "🔒 ATLAS utilise ici uniquement les outils de lecture Cubrik. Aucun ordre, agent ou réglage n'est modifié.",
    ])
    return "\n".join(lines)


def probe_native_loopback_registration():
    """Check whether Cubrik accepts an MCP native-client loopback redirect.

    This creates a public OAuth client registration only; it does not log in,
    request user consent, read data, or grant trading permissions.
    """
    import requests

    endpoint = "https://api.cubrik.ai/oauth/register"
    candidates = (
        "http://127.0.0.1:8765/callback",
        "http://localhost:8765/callback",
    )
    results = []
    for redirect_uri in candidates:
        payload = {
            "client_name": "ATLAS native read-only monitor",
            "application_type": "native",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=(5, 20),
            )
            if response.status_code in (200, 201):
                data = response.json()
                results.append({
                    "redirect_uri": redirect_uri,
                    "accepted": True,
                    "has_client_id": bool(data.get("client_id")),
                })
                break
            try:
                body = response.json()
            except ValueError:
                body = {}
            results.append({
                "redirect_uri": redirect_uri,
                "accepted": False,
                "status": response.status_code,
                "error": str(body.get("error") or "unknown")[:80],
                "description": str(body.get("error_description") or "")[:220],
            })
        except requests.RequestException as exc:
            results.append({
                "redirect_uri": redirect_uri,
                "accepted": False,
                "error": type(exc).__name__,
                "description": "Impossible de joindre l'endpoint OAuth Cubrik.",
            })
    return results


def _contains_oauth_registration_error(exc):
    if isinstance(exc, OAuthRegistrationError):
        return True
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, (list, tuple)):
        return any(_contains_oauth_registration_error(item) for item in nested)
    return False


def _oauth_redirect_error_message():
    return (
        "⚠️ Cubrik refuse l'adresse de retour OAuth configurée pour ATLAS.\n\n"
        "Adresse à faire autoriser par le support Cubrik :\n"
        + _redirect_uri()
        + "\n\n"
        "Demande-leur d'ajouter cette URL aux redirections MCP autorisées pour les applications personnalisées, "
        "ou de fournir une URL de callback officiellement prise en charge. "
        "Aucune autorisation n'a été accordée et aucun ordre n'a été envoyé."
    )


def _background_worker(chat_id, notify, detailed=True, oauth_queue=None):
    global _ACTIVE_WORKER
    auth_url_holder = {"url": None, "state": None}

    async def on_authorization_url(auth_url):
        parsed = parse_qs(urlparse(auth_url).query)
        auth_url_holder["url"] = auth_url
        auth_url_holder["state"] = (parsed.get("state") or [None])[0]
        with _PENDING_LOCK:
            if _PENDING is not None:
                _PENDING["url"] = auth_url
                _PENDING["state"] = auth_url_holder["state"]
        notify(
            chat_id,
            "🔐 Pour relier ATLAS à Cubrik, ouvre ce lien puis autorise la lecture du compte et la consultation/gestion des agents si Cubrik sépare ces droits. Ne coche surtout pas la permission Trading : ATLAS ne doit pas passer d'ordres.\n\n" + auth_url,
        )

    try:
        snapshot = asyncio.run(_read_cubrik_snapshot(
            auth_url_callback=on_authorization_url,
            callback_result=oauth_queue,
            detailed=detailed,
        ))
        message = format_cubrik_snapshot(snapshot, detailed=detailed)
        notify(chat_id, message)
    except OAuthRegistrationError:
        redirect_uri = _redirect_uri()
        log.error("Cubrik MCP rejected the configured OAuth redirect URI: %s", redirect_uri)
        notify(
            chat_id,
            "⚠️ Cubrik refuse l'adresse de retour OAuth configurée pour ATLAS.\n\n"
            "Adresse à faire autoriser par le support Cubrik :\n"
            + redirect_uri
            + "\n\n"
            "Demande-leur d'ajouter cette URL aux redirections MCP autorisées pour les applications personnalisées, "
            "ou de fournir une URL de callback officiellement prise en charge. "
            "Aucune autorisation n'a été accordée et aucun ordre n'a été envoyé."
        )
    except CubrikMonitorError as exc:
        notify(chat_id, "⚠️ Connexion Cubrik : " + str(exc))
    except Exception as exc:
        if _contains_oauth_registration_error(exc):
            log.error("Cubrik MCP rejected ATLAS's OAuth redirect URI: %s", _redirect_uri())
            notify(chat_id, _oauth_redirect_error_message())
        else:
            log.exception("ATLAS Cubrik MCP operation failed (%s)", type(exc).__name__)
            notify(
                chat_id,
                "⚠️ ATLAS n'a pas réussi à lire Cubrik. Vérifie l'autorisation MCP et les logs Render. Aucun ordre n'a été envoyé.",
            )
    finally:
        with _PENDING_LOCK:
            _ACTIVE_WORKER = None
            if _PENDING is not None and _PENDING.get("worker") is threading.current_thread():
                _PENDING.clear()


def start_cubrik_request(chat_id, notify, detailed=True):
    """Run a Cubrik read-only request in a worker thread so Flask stays responsive."""
    global _ACTIVE_WORKER, _PENDING
    with _PENDING_LOCK:
        if _ACTIVE_WORKER is not None and _ACTIVE_WORKER.is_alive():
            return False, "Une connexion Cubrik est déjà en cours. Termine l'autorisation dans le navigateur."
        oauth_queue = queue.Queue()
        worker = threading.Thread(
            target=_background_worker,
            args=(chat_id, notify, detailed, oauth_queue),
            daemon=True,
            name="atlas-cubrik-mcp",
        )
        _ACTIVE_WORKER = worker
        _PENDING = {
            "chat_id": str(chat_id),
            "queue": oauth_queue,
            "url": None,
            "state": None,
            "worker": worker,
        }
        worker.start()
    return True, "Lecture Cubrik lancée."


def complete_oauth_callback(query):
    """Validate the OAuth state before passing the code to the MCP SDK."""
    with _PENDING_LOCK:
        pending = _PENDING
        if not pending or not pending.get("worker") or not pending["worker"].is_alive():
            raise CubrikMonitorError("Aucune autorisation Cubrik n'est en attente.")
        expected = pending.get("state")
        received = query.get("state")
        if not expected or not received or received != expected:
            raise CubrikMonitorError("État OAuth invalide. Relance /cubrikconnect depuis Telegram.")
        error = query.get("error")
        payload = {
            "code": query.get("code"),
            "state": received,
            "iss": query.get("iss"),
            "error": error,
        }
        if not error and not payload["code"]:
            raise CubrikMonitorError("Le serveur d'autorisation n'a renvoyé aucun code.")
        pending["queue"].put(payload)


def available_tool_names():
    """Return the names of stored MCP token fields without exposing any token."""
    return is_connected()
