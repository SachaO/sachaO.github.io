"""Read-only OKX account monitor for ATLAS.

Only an explicit allowlist of GET endpoints is implemented. There are no
order, transfer, leverage-setting, withdrawal, or other mutation functions.
"""
import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests


class OKXReadOnlyError(RuntimeError):
    pass


_ALLOWED_PATHS = {
    "/api/v5/account/balance",
    "/api/v5/account/config",
    "/api/v5/account/positions",
    "/api/v5/account/positions-history",
    "/api/v5/trade/orders-pending",
    "/api/v5/trade/orders-history",
    "/api/v5/trade/orders-history-archive",
    "/api/v5/trade/fills-history",
    "/api/v5/trade/fills-history-archive",
    "/api/v5/account/bills",
    "/api/v5/account/bills-archive",
    "/api/v5/asset/balances",
}


def _credentials():
    values = {
        "key": os.environ.get("OKX_READ_API_KEY", "").strip(),
        "secret": os.environ.get("OKX_READ_API_SECRET", "").strip(),
        "passphrase": os.environ.get("OKX_READ_PASSPHRASE", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise OKXReadOnlyError(
            "Connexion OKX non configurée : ajoute les trois identifiants "
            "de la clé API en lecture seule dans les variables Render."
        )
    return values


def _base_url():
    # OKX specifies eea.okx.com for accounts registered in the EEA.
    # Override only if the account's own OKX region requires another domain.
    return os.environ.get("OKX_API_BASE_URL", "https://eea.okx.com").strip().rstrip("/")


def _get(path, params=None):
    if path not in _ALLOWED_PATHS:
        raise OKXReadOnlyError("Endpoint refusé : cet observateur est limité à une liste d'API de lecture.")
    credentials = _credentials()
    params = params or {}
    query = urlencode([(k, str(v)) for k, v in params.items() if v is not None])
    request_path = path + (("?" + query) if query else "")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    prehash = timestamp + "GET" + request_path
    digest = hmac.new(
        credentials["secret"].encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    headers = {
        "OK-ACCESS-KEY": credentials["key"],
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": credentials["passphrase"],
        "Accept": "application/json",
    }
    simulated = os.environ.get("OKX_READ_SIMULATED", "0").strip()
    if simulated == "1":
        headers["x-simulated-trading"] = "1"

    try:
        response = requests.get(
            _base_url() + request_path,
            headers=headers,
            timeout=(5, 15),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise OKXReadOnlyError("Impossible de joindre OKX. Vérifie le domaine régional et la connexion réseau.") from exc
    except ValueError as exc:
        raise OKXReadOnlyError("OKX a renvoyé une réponse illisible.") from exc

    if not isinstance(payload, dict):
        raise OKXReadOnlyError("Format de réponse OKX inattendu.")
    if str(payload.get("code", "")) != "0":
        # Never echo credentials or the full request URL in a user-facing error.
        code = str(payload.get("code", "inconnu"))
        message = str(payload.get("msg", "Erreur API"))[:180]
        raise OKXReadOnlyError("OKX a refusé la lecture (code {}). {} Vérifie la clé Read, sa région et ses restrictions.".format(code, message))
    return payload.get("data") or []


def _safe_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _num(value, digits=2, signed=False):
    number = _safe_float(value)
    if number is None:
        return "non communiqué"
    sign = "+" if signed and number > 0 else ""
    return sign + ("{:,.%df}" % digits).format(number).replace(",", " ")


def _utc_time(value):
    try:
        stamp = int(value) / 1000
        return datetime.fromtimestamp(stamp, timezone.utc).astimezone().strftime("%d/%m %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "date inconnue"


def _account_balance():
    rows = _get("/api/v5/account/balance")
    if not rows:
        return {}
    return rows[0]


def _funding_balances():
    return _get("/api/v5/asset/balances", {"ccy": None})


def _positions():
    return _get("/api/v5/account/positions", {"instType": None, "instId": None})


def _pending_orders():
    return _get("/api/v5/trade/orders-pending", {"instType": None, "ordType": None, "state": None})


def _recent_fills():
    # OKX's fills-history endpoint covers the recent trading window.
    return _get("/api/v5/trade/fills-history", {"instType": None, "limit": "20"})


def _recent_orders():
    return _get("/api/v5/trade/orders-history", {"instType": None, "state": None, "limit": "20"})


def _fills_archive():
    return _get("/api/v5/trade/fills-history-archive", {"instType": None, "limit": "20"})


def _orders_archive():
    return _get("/api/v5/trade/orders-history-archive", {"instType": None, "state": None, "limit": "20"})


def _positions_history():
    return _get("/api/v5/account/positions-history", {"instType": None, "limit": "20"})


def _recent_bills():
    return _get("/api/v5/account/bills", {"limit": "20"})


def _account_config():
    rows = _get("/api/v5/account/config")
    return rows[0] if rows else {}


def fetch_account_snapshot():
    """Fetch a factual, read-only snapshot; fail closed if required calls fail."""
    balance = _account_balance()
    if not balance:
        raise OKXReadOnlyError("OKX n'a retourné aucun résumé de compte.")
    details = balance.get("details") or []
    funding = _funding_balances()
    positions = _positions()
    pending = _pending_orders()
    fills = _recent_fills()
    orders = _recent_orders()
    config = _account_config()
    return {
        "captured_at": datetime.now().astimezone().strftime("%d/%m/%Y à %H:%M:%S"),
        "equity_usd": balance.get("totalEq"),
        "available_equity": balance.get("availEq"),
        "trading_assets": details,
        "funding_assets": funding,
        "positions": positions,
        "pending_orders": pending,
        "fills": fills,
        "orders": orders,
        "account_mode": config.get("acctLv"),
        "position_mode": config.get("posMode"),
        "auto_loan": config.get("autoLoan"),
        "currency": balance.get("uTime"),
    }


def fetch_history_snapshot():
    """Read-only recent activity: up to 3 months of fills/orders and 7 days of bills."""
    return {
        "fills": _fills_archive(),
        "orders": _orders_archive(),
        "positions_history": _positions_history(),
        "bills": _recent_bills(),
        "captured_at": datetime.now().astimezone().strftime("%d/%m/%Y à %H:%M:%S"),
    }


def format_history_snapshot(data):
    lines = [
        "🧾 ATLAS — HISTORIQUE OKX",
        "Lecture seule · Relevé du {}".format(data["captured_at"]),
        "",
        "Les historiques disponibles dépendent des limites de conservation et de pagination d'OKX.",
        "",
        "🔁 EXÉCUTIONS (jusqu'à 3 mois)",
    ]
    fills = data.get("fills") or []
    if not fills:
        lines.append("Aucune exécution renvoyée sur cette période.")
    for fill in fills[:12]:
        lines.append("• {date} — {inst} {side} | {size} à {price} | frais {fee} {fee_ccy}".format(
            date=_utc_time(fill.get("ts")),
            inst=fill.get("instId", "?"),
            side=fill.get("side", "?"),
            size=fill.get("fillSz", "?"),
            price=fill.get("fillPx", "?"),
            fee=fill.get("fee", "?"),
            fee_ccy=fill.get("feeCcy", ""),
        ))

    orders = data.get("orders") or []
    lines.extend(["", "📚 ORDRES (jusqu'à 3 mois)"])
    if not orders:
        lines.append("Aucun ordre historique renvoyé.")
    for order in orders[:10]:
        lines.append("• {date} — {inst} {side} | {state} | rempli {filled}/{size} | prix moyen {avg}".format(
            date=_utc_time(order.get("uTime") or order.get("cTime")),
            inst=order.get("instId", "?"),
            side=order.get("side", "?"),
            state=order.get("state", "?"),
            filled=order.get("accFillSz", "0"),
            size=order.get("sz", "?"),
            avg=_num(order.get("avgPx"), 6),
        ))

    position_history = data.get("positions_history") or []
    lines.extend(["", "🗂️ POSITIONS CLÔTURÉES / MODIFIÉES (jusqu'à 3 mois)"])
    if not position_history:
        lines.append("Aucune donnée d'historique de position renvoyée par OKX.")
    for item in position_history[:8]:
        lines.append("• {date} — {inst} {side} | taille {size} | P&L réalisé {pnl} USD".format(
            date=_utc_time(item.get("uTime") or item.get("cTime")),
            inst=item.get("instId", "?"),
            side=item.get("方向") or item.get("posSide", "?"),
            size=item.get("closeTotalPos") or item.get("pos", "?"),
            pnl=_num(item.get("realizedPnl") or item.get("pnl"), 2, signed=True),
        ))

    bills = data.get("bills") or []
    lines.extend(["", "💳 MOUVEMENTS DU COMPTE (7 derniers jours)"])
    if not bills:
        lines.append("Aucun mouvement renvoyé.")
    for bill in bills[:10]:
        lines.append("• {date} — {ccy} | variation {change} | solde {balance} | P&L {pnl} | frais {fee}".format(
            date=_utc_time(bill.get("ts")),
            ccy=bill.get("ccy", "?"),
            change=_num(bill.get("balChg"), 8, signed=True),
            balance=_num(bill.get("bal"), 8),
            pnl=_num(bill.get("pnl"), 8, signed=True),
            fee=_num(bill.get("fee"), 8, signed=True),
        ))
    lines.extend(["", "ℹ️ Les historiques sont limités par les fenêtres de disponibilité OKX ; les résultats affichés ne sont pas une comptabilité fiscale."])
    return "\n".join(lines)


def format_account_snapshot(data):
    """Present the account in plain French, with no inferred or invented values."""
    lines = [
        "📊 ATLAS — VUE DU COMPTE OKX",
        "Lecture seule · Relevé du {}".format(data["captured_at"]),
        "",
        "💰 CAPITAL ET DISPONIBILITÉS",
        "Valeur totale estimée par OKX : {} USD".format(_num(data.get("equity_usd"))),
        "Fonds propres disponibles : {} USD".format(_num(data.get("available_equity"))),
    ]

    trading_assets = data.get("trading_assets") or []
    if trading_assets:
        lines.append("")
        lines.append("Portefeuille de trading :")
        for asset in sorted(trading_assets, key=lambda x: _safe_float(x.get("eq"), 0) or 0, reverse=True)[:12]:
            ccy = asset.get("ccy", "?")
            total = asset.get("eq") or asset.get("cashBal") or "?"
            available = asset.get("availBal")
            line = "• {} : {} (disponible : {})".format(ccy, _num(total, 8), _num(available, 8))
            lines.append(line)
    else:
        lines.extend(["", "Aucun actif non nul renvoyé dans le compte de trading."])

    funding_assets = data.get("funding_assets") or []
    if funding_assets:
        lines.append("")
        lines.append("Compte de financement :")
        for asset in funding_assets[:10]:
            lines.append("• {} : {} (disponible : {})".format(
                asset.get("ccy", "?"),
                _num(asset.get("bal"), 8),
                _num(asset.get("availBal"), 8),
            ))

    positions = data.get("positions") or []
    lines.extend(["", "📈 POSITIONS OUVERTES ({})".format(len(positions))])
    if not positions:
        lines.append("Aucune position ouverte détectée.")
    else:
        for position in positions[:10]:
            side = position.get("posSide") or ("long" if (_safe_float(position.get("pos"), 0) or 0) > 0 else "short")
            lines.append(
                "• {inst} — {side} | taille {size} | entrée {entry} | prix {mark} | P&L latent {pnl} USD | levier x{lev} | liquidation {liq}".format(
                    inst=position.get("instId", "?"),
                    side=side,
                    size=position.get("pos", "?"),
                    entry=_num(position.get("avgPx"), 6),
                    mark=_num(position.get("markPx"), 6),
                    pnl=_num(position.get("upl"), 2, signed=True),
                    lev=position.get("lever") or "?",
                    liq=_num(position.get("liqPx"), 6),
                )
            )

    pending = data.get("pending_orders") or []
    lines.extend(["", "🕒 ORDRES EN ATTENTE ({})".format(len(pending))])
    if not pending:
        lines.append("Aucun ordre en attente détecté.")
    else:
        for order in pending[:10]:
            lines.append(
                "• {inst} | {side} {posside} | quantité {size} | exécuté {filled} | prix {price} | statut {state}".format(
                    inst=order.get("instId", "?"),
                    side=order.get("side", "?"),
                    posside=order.get("posSide", ""),
                    size=order.get("sz", "?"),
                    filled=order.get("accFillSz", "0"),
                    price=order.get("px") or "marché",
                    state=order.get("state", "?"),
                )
            )

    fills = data.get("fills") or []
    lines.extend(["", "🧾 EXÉCUTIONS (3 derniers jours · {})".format(len(fills))])
    if not fills:
        lines.append("Aucune exécution récente renvoyée par OKX.")
    else:
        for fill in fills[:8]:
            lines.append(
                "• {date} — {inst} {side} | {size} à {price} | frais {fee} {fee_ccy}".format(
                    date=_utc_time(fill.get("ts")),
                    inst=fill.get("instId", "?"),
                    side=fill.get("side", "?"),
                    size=fill.get("fillSz", "?"),
                    price=fill.get("fillPx", "?"),
                    fee=fill.get("fee", "?"),
                    fee_ccy=fill.get("feeCcy", ""),
                )
            )

    orders = data.get("orders") or []
    lines.extend(["", "📚 ORDRES TERMINÉS (7 derniers jours · {})".format(len(orders))])
    if not orders:
        lines.append("Aucun ordre terminé renvoyé sur la période disponible.")
    else:
        for order in orders[:8]:
            lines.append(
                "• {date} — {inst} {side} | statut {state} | rempli {filled}/{size} | prix moyen {avg}".format(
                    date=_utc_time(order.get("uTime") or order.get("cTime")),
                    inst=order.get("instId", "?"),
                    side=order.get("side", "?"),
                    state=order.get("state", "?"),
                    filled=order.get("accFillSz", "0"),
                    size=order.get("sz", "?"),
                    avg=_num(order.get("avgPx"), 6),
                )
            )

    lines.extend([
        "",
        "Mode du compte : {}".format(data.get("account_mode") or "non communiqué"),
        "Mode de position : {}".format(data.get("position_mode") or "non communiqué"),
        "",
        "ℹ️ Les montants sont ceux renvoyés par OKX. Le détail dépend des produits et des autorisations de lecture de la clé. Ce relevé n'exécute aucune action.",
    ])
    return "\n".join(lines)


def fetch_and_format_account():
    return format_account_snapshot(fetch_account_snapshot())
