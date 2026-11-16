# Serveur MCP Kraken indépendant

Ce service est séparé du bot Telegram ATLAS. Il expose un endpoint MCP Streamable HTTP protégé par jeton Bearer.

## État initial sécurisé
- Santé : GET /health
- MCP : /mcp
- Authentification MCP : Authorization: Bearer <MCP_ACCESS_TOKEN>
- Trading réel : non implémenté dans cette première version ; les outils sont en lecture seule.
- Marchés publics Spot et Futures, soldes/ordres Spot, comptes/positions/ordres Futures en lecture.

## Variables à saisir dans Render > service > Environment
- MCP_ACCESS_TOKEN : jeton aléatoire long, au moins 32 octets.
- KRAKEN_SPOT_API_KEY
- KRAKEN_SPOT_API_SECRET
- KRAKEN_FUTURES_API_KEY
- KRAKEN_FUTURES_API_SECRET
- KRAKEN_TRADING_ENABLED : false

Ne jamais placer les secrets dans GitHub ou dans une conversation. Les clés Spot et Futures sont distinctes. Pour les premiers tests, utiliser des clés API limitées à la lecture.

## Configuration Render
Runtime Python
Build : pip install -r kraken_mcp/requirements.txt
Start : uvicorn kraken_mcp.server:app --host 0.0.0.0 --port $PORT
Plan : Starter
Région : Frankfurt
