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
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

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
DEFAULT_CALLBACK_URI = "http://127.0.0.1:8765/callback"

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
    "analysis": ("get_analysis", "get_analytics"),
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
        granted_scopes = set((getattr(tokens, "scope", None) or "").split())
        if "cubrik.trade" in granted_scopes:
            raise CubrikMonitorError(
                "Cubrik a accordé la permission de trading à ATLAS. Le jeton a été refusé et n'a pas été enregistré."
            )
        if granted_scopes and not {"cubrik.read", "cubrik.agents"}.issubset(granted_scopes):
            raise CubrikMonitorError(
                "Le jeton Cubrik ne contient pas les permissions de lecture attendues. Il n'a pas été enregistré."
            )
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


async def _read_cubrik_snapshot(auth_url_callback=None, callback_result=None, detailed=True, agents_only=False):
    server_url = _server_url()
    redirect_uri = _redirect_uri()
    storage = PostgresOAuthStorage()

    async def redirect_handler(auth_url):
        parsed = urlparse(auth_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        requested_scopes = set((query.get("scope") or [""])[0].split())
        required_scopes = {"cubrik.read", "cubrik.agents"}
        if not required_scopes.issubset(requested_scopes):
            raise CubrikMonitorError(
                "La demande OAuth Cubrik ne contient pas les permissions de lecture et d'accès aux agents attendues. "
                "Aucun lien n'a été transmis."
            )

        # Cubrik advertises a combined scope list that also includes cubrik.trade.
        # ATLAS explicitly narrows the authorization request to read + agent visibility.
        safe_scopes = required_scopes | ({"offline_access"} if "offline_access" in requested_scopes else set())
        safe_scope_value = " ".join(
            scope for scope in ("cubrik.read", "cubrik.agents", "offline_access")
            if scope in safe_scopes
        )
        query["scope"] = [safe_scope_value]

        # Keep the SDK's token-response scope validation and storage aligned with
        # the narrowed authorization request, not the wider metadata advertisement.
        oauth.context.client_metadata.scope = safe_scope_value
        safe_auth_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

        if auth_url_callback:
            await auth_url_callback(safe_auth_url)
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
            scope="cubrik.read cubrik.agents",
            application_type="native",
            token_endpoint_auth_method="none",
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

                # Public group mode is a concise trading journal: account equity,
                # open positions, realized analytics, closed trades and the latest
                # executor run. It never reads subscription or credit information.
                if agents_only:
                    jobs = [
                        ("agents", {}),
                        ("portfolio", {"refresh": True}),
                    ]
                else:
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
                if agents_only:
                    # OKX portfolio gives the latest equity and current positions.
                    # get_analysis supplies the reporting currency (USD on OKX).
                    portfolio_items = _portfolio_accounts(responses.get("portfolio"))
                    okx_portfolio = next(
                        (
                            item for item in portfolio_items
                            if str(item.get("name") or item.get("platform") or "").upper() == "OKX"
                        ),
                        portfolio_items[0] if portfolio_items else {},
                    )
                    analysis_value, analysis_warning = await _call_read_tool(
                        session,
                        tools,
                        "analysis",
                        {
                            "account": okx_portfolio.get("id") or "OKX",
                            "period": "week",
                        },
                    )
                    if analysis_warning:
                        warnings.append(analysis_warning)
                    analysis = analysis_value or {}
                    public_capital = okx_portfolio.get("equity")
                    if public_capital is None:
                        public_capital = okx_portfolio.get("balance")
                    public_currency = (
                        analysis.get("currency")
                        if isinstance(analysis, dict) else None
                    ) or okx_portfolio.get("currency")

                    snapshot["public_capital"] = public_capital
                    snapshot["public_currency"] = public_currency
                    snapshot["public_positions"] = _extract_positions(responses.get("portfolio"))
                    snapshot["analysis"] = analysis

                    history_args = {"days": 7, "limit": 10}
                    if okx_portfolio.get("id"):
                        history_args["account"] = okx_portfolio["id"]
                    history_value, history_warning = await _call_read_tool(
                        session, tools, "history", history_args
                    )
                    if history_value is not None:
                        snapshot["history"] = history_value
                    if history_warning:
                        warnings.append(history_warning)

                    # Find the only trade-enabled executor internally; the agent roster
                    # itself is not included in the public Telegram message.
                    executor = next(
                        (
                            agent for agent in agents
                            if str(agent.get("name") or "").upper() == "ATLAS-SWING-EXECUTOR"
                        ),
                        None,
                    )
                    executor_id = (
                        executor.get("id") or executor.get("agent_id")
                        if isinstance(executor, dict) else None
                    )
                    if executor_id and _tool_has_name(tools, "agent_runs"):
                        run_value, run_warning = await _call_read_tool(
                            session,
                            tools,
                            "agent_runs",
                            {"agent_id": str(executor_id), "limit": 3},
                        )
                        if run_value is not None:
                            snapshot["executor_runs"] = run_value
                        if run_warning:
                            warnings.append(run_warning)

                    # Publicly show the market analyst's latest scan as ideas only.
                    # The scanner is read-only; its output is never described as an order.
                    scanner = next(
                        (
                            agent for agent in agents
                            if str(agent.get("name") or "").upper() == "ATLAS-MARKET-SCANNER"
                        ),
                        None,
                    )
                    scanner_id = (
                        scanner.get("id") or scanner.get("agent_id")
                        if isinstance(scanner, dict) else None
                    )
                    if scanner_id and _tool_has_name(tools, "agent_runs"):
                        scanner_value, scanner_warning = await _call_read_tool(
                            session,
                            tools,
                            "agent_runs",
                            {"agent_id": str(scanner_id), "limit": 3},
                        )
                        if scanner_value is not None:
                            snapshot["scanner_runs"] = scanner_value
                        if scanner_warning:
                            warnings.append(scanner_warning)

                    snapshot["warnings"] = warnings
                    return snapshot

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


def _amount_label(value, currency=None):
    label = _money(value)
    return label + ((" " + str(currency)) if currency else " (unité non communiquée)")


def _pnl_badge(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "⚪ non communiqué"
    if number > 0:
        return "🟢 +{}".format(_money(number))
    if number < 0:
        return "🔴 {}".format(_money(number))
    return "⚪ 0,00"


def _public_trade_side(value):
    value = str(value or "opération").strip().lower()
    labels = {
        "buy": "Achat",
        "long": "Achat",
        "sell": "Vente",
        "short": "Vente",
    }
    return labels.get(value, value.capitalize())


def _format_run_timestamp(value):
    if not value:
        return "heure inconnue"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%d/%m à %H:%M")
    except (TypeError, ValueError):
        return str(value)


def _format_public_positions(positions, currency):
    if not positions:
        return ["📂 Positions ouvertes : aucune"]

    lines = ["📂 POSITIONS EN COURS"]
    for position in positions[:6]:
        symbol = _pick(position, ("instrument", "instId", "symbol", "market")) or "Actif inconnu"
        side = _pick(position, ("side", "direction", "posSide"))
        size = _pick(position, ("size", "pos", "quantity", "qty", "contracts"))
        entry = _pick(position, ("entry_price", "avgPx", "average_price", "avg_price"))
        pnl = _pick(position, ("unrealized_pnl", "unrealizedPnl", "upl"))
        pieces = [str(symbol)]
        if side:
            pieces.append(str(side).upper())
        if size is not None:
            pieces.append("taille {}".format(_short_value(size, 35)))
        if entry is not None:
            pieces.append("entrée {}".format(_short_value(entry, 35)))
        if pnl is not None:
            pieces.append("P&L latent {} {}".format(_pnl_badge(pnl), currency or ""))
        lines.append("• " + " · ".join(pieces))
    return lines


def _metric_number(value, suffix="", decimals=2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return ("{:,.{}f}".format(number, decimals).replace(",", " ") + suffix)


def _format_public_trade_activity(history_payload, analysis_payload, currency):
    trades = _items(history_payload, ("trades", "history", "items", "data", "fills"))
    analysis = analysis_payload if isinstance(analysis_payload, dict) else {}
    kpis = analysis.get("kpis") if isinstance(analysis.get("kpis"), dict) else {}
    net_result = _pick(kpis, ("netResult", "net_result", "realizedPnl", "realized_pnl"))
    net_pct = _pick(kpis, ("netResultPct", "net_result_pct", "returnPct", "return_pct"))
    trade_count = _pick(kpis, ("tradeCount", "trade_count", "closedTrades"))
    win_rate = _pick(kpis, ("winRatePct", "win_rate_pct", "winRate", "win_rate"))
    profit_factor = _pick(kpis, ("profitFactor", "profit_factor"))
    max_drawdown_pct = _pick(kpis, ("maxDrawdownPct", "max_drawdown_pct"))
    currency = currency or analysis.get("currency") or "USD"

    try:
        count = int(trade_count or 0)
    except (TypeError, ValueError):
        count = 0

    lines = [
        "📊 PERFORMANCE RÉALISÉE — 7 JOURS",
        "Trades clôturés : {}".format(count),
    ]

    if count == 0:
        lines.append("Résultat réalisé : aucun trade clôturé sur la période.")
        lines.append("Statistiques de performance : non significatives (échantillon nul).")
        return lines

    if net_result is not None:
        lines.append("Résultat net : {} {}".format(_pnl_badge(net_result), currency))
    else:
        lines.append("Résultat net : non communiqué par Cubrik.")

    if net_pct is not None:
        rendered_pct = _metric_number(net_pct, "%", 2)
        if rendered_pct is not None:
            lines.append("Variation réalisée : {}".format(rendered_pct))

    # Metrics are mathematically reportable with a modest sample, but label the
    # sample size so no one mistakes a few trades for robust statistical evidence.
    if count >= 5:
        if win_rate is not None:
            try:
                win_rate_number = float(win_rate)
                if 0 <= win_rate_number <= 1:
                    win_rate_number *= 100
                lines.append("Trades gagnants : {} (sur {})".format(
                    _metric_number(win_rate_number, "%", 1), count
                ))
            except (TypeError, ValueError):
                pass
        if profit_factor is not None:
            lines.append("Profit factor : {}".format(_metric_number(profit_factor, "", 2) or "non communiqué"))
        if max_drawdown_pct is not None:
            lines.append("Drawdown maximal : {}%".format(_metric_number(max_drawdown_pct, "", 2) or "non communiqué"))
    else:
        lines.append("Taux de réussite / profit factor : échantillon trop faible (moins de 5 trades).")

    if trades:
        lines.append("DERNIÈRES OPÉRATIONS CLÔTURÉES")
        for trade in trades[:5]:
            if not isinstance(trade, dict):
                continue
            instrument = _pick(trade, ("instrument", "symbol", "instId", "market")) or "Actif inconnu"
            side = _public_trade_side(_pick(trade, ("side", "direction", "action")))
            pnl = _pick(
                trade,
                ("pnl", "profit_loss", "realized_pnl", "realizedPnl", "net_pnl", "netPnl"),
            )
            at = _pick(trade, ("closed_at", "closedAt", "at", "created_at", "timestamp", "time"))
            trade_currency = _pick(trade, ("currency", "pnl_currency", "quote_currency")) or currency
            line = "• {} — {} — {}".format(instrument, side, _pnl_badge(pnl))
            if pnl is not None and trade_currency:
                line += " " + str(trade_currency)
            if at:
                line += " — " + _format_run_timestamp(at)
            lines.append(line)
    else:
        lines.append("Cubrik fournit les indicateurs agrégés, mais pas le détail des opérations.")
    return lines


def _latest_real_run(value):
    runs = _items(value, ("runs", "items", "history", "data"))
    if not runs and isinstance(value, dict):
        runs = [value]
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("test") is True:
            continue
        if str(run.get("status") or "").lower() not in ("succeeded", "running", "completed"):
            continue
        return run
    return None


def _format_public_scanner_activity(value):
    run = _latest_real_run(value)
    if not run:
        return [
            "🔎 OPPORTUNITÉS REPÉRÉES PAR LE SCANNER",
            "Pas encore de scan réel récent disponible. Les idées sont des pistes, pas des ordres.",
        ]

    lines = [
        "🔎 OPPORTUNITÉS REPÉRÉES PAR LE SCANNER — {}".format(
            _format_run_timestamp(run.get("at") or run.get("started_at") or run.get("timestamp"))
        )
    ]
    candidates = run.get("decisions") or run.get("opportunities") or run.get("candidates") or []
    if candidates:
        for candidate in candidates[:3]:
            if not isinstance(candidate, dict):
                continue
            instrument = _pick(candidate, ("symbol", "instrument", "market", "name")) or "Instrument"
            side = _pick(candidate, ("side", "direction", "bias"))
            status = _pick(candidate, ("status", "setup_status", "state"))
            reason = _pick(candidate, ("reason", "thesis", "summary", "why"))
            pieces = [str(instrument)]
            if side:
                pieces.append(str(side).upper())
            if status:
                pieces.append(str(status))
            if reason:
                pieces.append(_short_value(reason, 115))
            lines.append("• " + " — ".join(pieces))
    summary = _pick(run, ("summary", "message", "result"))
    if summary:
        text = str(summary).strip().split("\n\n", 1)[0].replace("\n", " ")
        lines.append(_short_value(text, 520))
    next_step = _pick(run, ("next", "next_steps", "watch"))
    if next_step:
        lines.append("À surveiller : " + _short_value(next_step, 180))
    lines.append("Ce sont des idées de marché ; l'exécuteur doit les vérifier avant tout ordre.")
    return lines


def _format_public_executor_activity(value):
    run = _latest_real_run(value)
    if not run:
        return [
            "⚙️ DÉCISION DE L'EXÉCUTEUR",
            "Aucune exécution réelle récente renvoyée par Cubrik.",
        ]

    lines = [
        "⚙️ DÉCISION DE L'EXÉCUTEUR — {}".format(
            _format_run_timestamp(run.get("at") or run.get("started_at") or run.get("timestamp"))
        )
    ]
    decisions = run.get("decisions") or []
    actionable = []
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        action = str(decision.get("action") or "review").lower()
        if action not in ("skip", "wait", "hold", "none", "review"):
            actionable.append(decision)

    if actionable:
        for decision in actionable[:3]:
            instrument = _pick(decision, ("symbol", "instrument", "market")) or "Instrument"
            action = str(decision.get("action") or "").lower()
            side = _public_trade_side(_pick(decision, ("side", "direction", "action")))
            state = _pick(decision, ("order_status", "orderStatus", "status", "execution_status"))
            lines.append("• {} — {}{}".format(
                instrument,
                side,
                (" — statut : " + str(state)) if state else " — décision prise",
            ))
            reason = _pick(decision, ("reason", "explanation"))
            if reason:
                lines.append("  " + _short_value(reason, 150))
    elif decisions:
        lines.append("Aucune entrée validée : l'exécuteur a choisi d'attendre.")
        reasons = []
        for decision in decisions[:3]:
            if isinstance(decision, dict) and _pick(decision, ("reason", "explanation")):
                reasons.append(_short_value(_pick(decision, ("reason", "explanation")), 120))
        if reasons:
            lines.append("Motif : " + " ; ".join(reasons[:2]))
    else:
        summary = _pick(run, ("summary", "message", "result"))
        if summary:
            paragraph = str(summary).strip().split("\n\n", 1)[0].replace("\n", " ")
            lines.append(_short_value(paragraph, 330))
        else:
            lines.append("Aucune décision détaillée n'est disponible.")

    orders = run.get("orders") or run.get("placed_orders") or []
    for order in orders[:3] if isinstance(orders, list) else []:
        if isinstance(order, dict):
            instrument = _pick(order, ("symbol", "instrument", "instId")) or "Instrument"
            status = _pick(order, ("status", "state", "order_status")) or "statut inconnu"
            lines.append("Ordre {} : {}".format(instrument, status))
    if run.get("error"):
        lines.append("⚠️ Exécution signalée en erreur.")
    return lines


def format_cubrik_agents_snapshot(data):
    currency = data.get("public_currency") or "USD"
    capital = data.get("public_capital")
    analysis = data.get("analysis") or {}
    positions = data.get("public_positions") or []

    lines = [
        "🤖 ATLAS — SUIVI DU COMPTE OKX",
        "Relevé : {}".format(data.get("captured_at", "heure inconnue")),
        "",
        "💰 Capital total : {} {}".format(
            _money(capital) if capital is not None else "non communiqué",
            currency,
        ),
    ]
    lines.extend(_format_public_positions(positions, currency))
    lines.extend([""] + _format_public_trade_activity(
        data.get("history"),
        analysis,
        currency,
    ))
    lines.extend([""] + _format_public_scanner_activity(data.get("scanner_runs")))
    lines.extend([""] + _format_public_executor_activity(data.get("executor_runs")))

    if data.get("warnings"):
        lines.append("")
        lines.extend("⚠️ " + warning for warning in data["warnings"][:3])

    return "\n".join(lines)


def format_cubrik_snapshot(data, detailed=True):
    account = _account_data(data.get("account"))
    agents_summary = account.get("agents") or {}
    platforms = _extract_platforms(data.get("platforms"))
    agents = _extract_agents(data.get("agents"))
    positions = _extract_positions(data.get("portfolio"))
    notifications = _items(data.get("notifications"), ("notifications", "items", "data"))

    if not detailed:
        portfolio_accounts = _portfolio_accounts(data.get("portfolio"))
        account_entry = next(
            (item for item in portfolio_accounts if str(item.get("name", "")).upper() == "OKX"),
            portfolio_accounts[0] if portfolio_accounts else {},
        )
        capital = account_entry.get("equity")
        if capital is None:
            capital = account_entry.get("balance")
        currency = account_entry.get("currency")

        results_payload = data.get("results") or {}
        result_accounts = _items(results_payload, ("accounts", "items", "data"))
        okx_result = next(
            (item for item in result_accounts if str(item.get("account", item.get("name", ""))).upper() == "OKX"),
            result_accounts[0] if result_accounts else {},
        )
        realized = _pick(okx_result, ("netResult", "net_result", "realizedPnl", "realized_pnl"))
        realized_currency = okx_result.get("currency") or currency

        if positions:
            latent_values = [
                _pick(position, ("unrealized_pnl", "unrealizedPnl", "upl"))
                for position in positions
            ]
            try:
                latent = sum(float(value) for value in latent_values)
            except (TypeError, ValueError):
                latent = None
        else:
            latent = 0.0

        active_count = sum(
            1 for agent in agents
            if str(agent.get("status", "")).lower() in ("active", "running")
        )
        lines = [
            "🤖 ATLAS — PORTEFEUILLE OKX",
            "Relevé : {}".format(data.get("captured_at", "heure inconnue")),
            "",
            "💰 Capital total : {}".format(
                _amount_label(capital, currency) if capital is not None else "non communiqué"
            ),
            "📈 Plus-value réalisée (7 derniers jours) : {}".format(
                _pnl_badge(realized) +
                ((" " + str(realized_currency)) if realized_currency else
                 (" (unité non communiquée)" if realized is not None else ""))
            ),
            "📊 P&L latent : {}".format(
                _pnl_badge(latent) +
                ((" " + str(currency)) if currency else
                 (" (unité non communiquée)" if latent is not None else ""))
            ),
            "📂 Positions ouvertes : {}".format(len(positions)),
            "🧠 Agents actifs : {} / {}".format(active_count, len(agents)),
        ]
        if data.get("warnings"):
            lines.append("")
            lines.extend("⚠️ " + warning for warning in data["warnings"][:2])
        if not positions and realized == 0:
            lines.extend([
                "",
                "Le compte OKX est connecté. Aucune position n'est actuellement ouverte et aucun trade n'a été clôturé sur les 7 derniers jours. L'agent swing reste actif et attend un signal conforme à ses règles de risque ; il n'a pas besoin de prendre une position à chaque analyse.",
            ])
        elif positions:
            lines.extend([
                "",
                "Le portefeuille comporte des positions ouvertes. Le P&L latent évolue avec le marché et ne devient réalisé qu'à la clôture.",
            ])
        return "\n".join(lines)

    lines = [
        "🤖 ATLAS — ÉTAT DE CUBRIK",
        "Relevé : {}".format(data.get("captured_at", "heure inconnue")),
    ]

    plan = account.get("plan") or account.get("subscription") or "non communiqué"
    lines.extend([
        "",
        "📦 COMPTE",
        "Forfait : {}".format(plan),
        "Agents actifs : {} / {}".format(
            agents_summary.get("active", sum(1 for a in agents if str(a.get("status", "")).lower() in ("active", "running"))),
            agents_summary.get("limit", "?"),
        ),
    ])

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


def _background_worker(chat_id, notify, detailed=True, oauth_queue=None, agents_only=False):
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
            "🔐 Connexion Cubrik — étape 1 : lance le fichier atlas_cubrik_oauth_relay.py sur le même PC que ton navigateur et laisse la fenêtre ouverte.\n\nÉtape 2 : ouvre ce lien depuis CE MÊME PC, puis autorise uniquement la lecture du compte et des agents. Refuse toute permission de trading. Le navigateur reviendra sur 127.0.0.1 et le relais transmettra le code à Render.\n\n" + auth_url,
        )

    try:
        snapshot = asyncio.run(_read_cubrik_snapshot(
            auth_url_callback=on_authorization_url,
            callback_result=oauth_queue,
            detailed=detailed,
            agents_only=agents_only,
        ))
        message = (
            format_cubrik_agents_snapshot(snapshot)
            if agents_only else
            format_cubrik_snapshot(snapshot, detailed=detailed)
        )
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


def start_cubrik_request(chat_id, notify, detailed=True, agents_only=False):
    """Run a Cubrik read-only request in a worker thread so Flask stays responsive."""
    global _ACTIVE_WORKER, _PENDING
    with _PENDING_LOCK:
        if _ACTIVE_WORKER is not None and _ACTIVE_WORKER.is_alive():
            return False, "Une connexion Cubrik est déjà en cours. Termine l'autorisation dans le navigateur."
        oauth_queue = queue.Queue()
        worker = threading.Thread(
            target=_background_worker,
            args=(chat_id, notify, detailed, oauth_queue, agents_only),
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
