import base64
import hashlib
import hmac
import os
import time
from typing import Any
from urllib.parse import urlencode

import requests
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from mcp.server.fastmcp import FastMCP

SPOT_BASE = "https://api.kraken.com"
FUTURES_BASE = "https://futures.kraken.com"
TIMEOUT = 20

mcp = FastMCP("Kraken MCP - Spot, Margin et Derivatives")


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variable Render manquante : {name}")
    return value


def _spot_private(uri_path: str, data: dict[str, Any] | None = None) -> dict:
    api_key = _required_env("KRAKEN_SPOT_API_KEY")
    api_secret = _required_env("KRAKEN_SPOT_API_SECRET")
    payload = dict(data or {})
    payload["nonce"] = str(time.time_ns() // 1_000_000)
    postdata = urlencode(payload)
    encoded = (payload["nonce"] + postdata).encode()
    message = uri_path.encode() + hashlib.sha256(encoded).digest()
    secret = base64.b64decode(api_secret)
    signature = base64.b64encode(hmac.new(secret, message, hashlib.sha512).digest()).decode()
    response = requests.post(
        SPOT_BASE + uri_path,
        data=payload,
        headers={"API-Key": api_key, "API-Sign": signature},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("error"):
        raise RuntimeError("; ".join(result["error"]))
    return result.get("result", {})


def _futures_private(endpoint: str, data: dict[str, Any] | None = None) -> dict:
    api_key = _required_env("KRAKEN_FUTURES_API_KEY")
    api_secret = _required_env("KRAKEN_FUTURES_API_SECRET")
    nonce = str(time.time_ns() // 1_000_000)
    payload = dict(data or {})
    postdata = urlencode(payload)
    path = "/api/v3/" + endpoint.lstrip("/")
    digest = hashlib.sha256((postdata + nonce).encode()).digest()
    message = path.encode() + digest
    secret = base64.b64decode(api_secret)
    authent = base64.b64encode(hmac.new(secret, message, hashlib.sha512).digest()).decode()
    response = requests.post(
        FUTURES_BASE + path,
        data=payload,
        headers={"APIKey": api_key, "Authent": authent, "Nonce": nonce},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("result") == "error" or result.get("errors"):
        raise RuntimeError(str(result))
    return result


def _public_get(url: str, params: dict[str, Any] | None = None) -> dict:
    response = requests.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    result = response.json()
    if result.get("error"):
        raise RuntimeError("; ".join(result["error"]))
    return result.get("result", result)


@mcp.tool()
def kraken_etat_serveur() -> dict:
    """Vérifie l'état du connecteur et indique si le trading réel est verrouillé."""
    return {
        "service": "Kraken MCP",
        "trading_enabled": os.getenv("KRAKEN_TRADING_ENABLED", "false").lower() == "true",
        "mode": "lecture seule" if os.getenv("KRAKEN_TRADING_ENABLED", "false").lower() != "true" else "trading activé",
        "cles_spot_configurees": bool(os.getenv("KRAKEN_SPOT_API_KEY") and os.getenv("KRAKEN_SPOT_API_SECRET")),
        "cles_derivatives_configurees": bool(os.getenv("KRAKEN_FUTURES_API_KEY") and os.getenv("KRAKEN_FUTURES_API_SECRET")),
    }


@mcp.tool()
def kraken_spot_ticker(pair: str) -> dict:
    """Récupère le ticker public Spot Kraken pour une paire, ex. XBTUSD ou ETHUSD."""
    return _public_get(SPOT_BASE + "/0/public/Ticker", {"pair": pair})


@mcp.tool()
def kraken_spot_paires(actives_uniquement: bool = True) -> dict:
    """Liste les marchés Spot Kraken disponibles."""
    result = _public_get(SPOT_BASE + "/0/public/AssetPairs")
    if actives_uniquement:
        result = {k: v for k, v in result.items() if v.get("status") == "online"}
    return result


@mcp.tool()
def kraken_spot_solde() -> dict:
    """Lit les soldes Spot du compte Kraken. Nécessite une clé API Spot avec permission de lecture des soldes."""
    return _spot_private("/0/private/Balance")


@mcp.tool()
def kraken_spot_ordres_ouverts() -> dict:
    """Lit les ordres Spot ouverts. Nécessite une clé API Spot avec permission de lecture des ordres."""
    return _spot_private("/0/private/OpenOrders")


@mcp.tool()
def kraken_derivatives_instruments() -> dict:
    """Liste les contrats Kraken Futures/Derivatives disponibles."""
    return _public_get(FUTURES_BASE + "/derivatives/api/v3/instruments")


@mcp.tool()
def kraken_derivatives_ticker(symbol: str) -> dict:
    """Récupère les informations publiques d'un contrat Futures, ex. PI_XBTUSD."""
    return _public_get(FUTURES_BASE + "/derivatives/api/v3/tickers", {"symbol": symbol})


@mcp.tool()
def kraken_derivatives_comptes() -> dict:
    """Lit les portefeuilles Kraken Futures. Nécessite une clé API Futures en lecture."""
    return _futures_private("accounts")


@mcp.tool()
def kraken_derivatives_positions_ouvertes() -> dict:
    """Lit les positions ouvertes Kraken Futures. Nécessite une clé API Futures en lecture."""
    return _futures_private("openpositions")


@mcp.tool()
def kraken_derivatives_ordres_ouverts() -> dict:
    """Lit les ordres ouverts Kraken Futures. Nécessite une clé API Futures en lecture."""
    return _futures_private("openorders")


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "kraken-mcp", "trading_enabled": False})


class BearerAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            expected = os.getenv("MCP_ACCESS_TOKEN", "").strip()
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode(errors="ignore")
            if not expected or not hmac.compare_digest(auth, "Bearer " + expected):
                response = JSONResponse({"error": "Authentification MCP requise"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


mcp_http_app = mcp.streamable_http_app()
inner = Starlette(routes=[
    Route("/health", health, methods=["GET"]),
    Mount("/mcp", app=mcp_http_app),
])
app = BearerAuthMiddleware(inner)
