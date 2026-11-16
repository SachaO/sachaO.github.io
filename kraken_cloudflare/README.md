# Kraken MCP sur Cloudflare Workers (gratuit)

Ce dossier est un projet distinct du bot Telegram. Il utilise le modèle MCP distant Streamable HTTP de Cloudflare.

## Fonctionnalités de cette première version
- Endpoint MCP protégé par Bearer token : /mcp
- Santé : /health
- Outils en lecture seule : état du connecteur, tickers et paires Spot, soldes et ordres Spot, instruments et comptes/positions Futures.
- Aucun outil d'envoi, modification ou annulation d'ordre. Trading désactivé par conception.

## Déploiement
1. Créer un compte Cloudflare (Workers Free).
2. Dans Workers & Pages, choisir Create > Workers > Import a repository.
3. Dépôt : SachaO/sachaO.github.io
4. Branche : kraken-cloudflare-mcp
5. Répertoire du projet / root directory : kraken_cloudflare
6. Runtime : Node.js. Build/deploy selon l'assistant Cloudflare ; utiliser npm install puis npx wrangler deploy si demandé.
7. Dans les paramètres du Worker > Settings > Variables and Secrets, ajouter en tant que secrets (pas variables en clair) :
   - MCP_ACCESS_TOKEN : jeton aléatoire long
   - KRAKEN_SPOT_API_KEY
   - KRAKEN_SPOT_API_SECRET
   - KRAKEN_FUTURES_API_KEY
   - KRAKEN_FUTURES_API_SECRET
8. Déployer. Le serveur sera accessible à https://NOM-DU-WORKER.COMPTE.workers.dev/mcp et /health.

Ne jamais committer de secrets dans GitHub, ne jamais les envoyer dans ChatGPT. Les clés Spot et Futures sont séparées. Pour commencer, utiliser des clés Kraken limitées à la lecture et sans permission de retrait.

## Limites connues
- Le Worker ne tourne pas en boucle : il traite les appels MCP entrants. C'est suffisant pour un serveur MCP appelé à la demande, pas pour un bot qui doit scanner les marchés en continu sans requête.
- Le plan Workers Free affiche une limite de 100 000 requêtes par jour et 10 ms de CPU par invocation ; les requêtes réseau vers Kraken ne comptent pas comme CPU actif. Vérifier les quotas Cloudflare à la date du déploiement.
- Le serveur n'active pas le trading. Les outils d'exécution Spot/Margin/Futures nécessitent une implémentation distincte, des tests de signatures et de paramètres d'ordre, puis une validation de sécurité.

## Connexion à ChatGPT
L'URL /mcp est un endpoint MCP distant. Le plugin Kraken Trader Assistant existant est un paquet d'instructions et ne se connecte pas automatiquement à un serveur MCP par simple mise à jour de son texte. Il faudra ajouter le serveur comme connecteur MCP dans une interface ChatGPT qui prend en charge les serveurs MCP distants, puis vérifier que les outils sont effectivement visibles.
