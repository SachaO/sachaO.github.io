# Connexion ATLAS à Cubrik — procédure Windows

Cette procédure ne demande qu'une autorisation initiale dans Cubrik. Après validation, Render conserve les jetons OAuth chiffrés et les renouvelle automatiquement lorsque Cubrik permet le rafraîchissement.

## Pourquoi une connexion est encore nécessaire

Une connexion Cubrik déjà autorisée dans ChatGPT appartient à ChatGPT. Render est une application indépendante : il ne reçoit ni le cookie de ton navigateur, ni les jetons OAuth de ChatGPT. Il doit donc obtenir son propre accès une première fois.

## Première connexion

1. Vérifie que le service Render sachao-github-io est LIVE et que CUBRIK_MCP_REDIRECT_URI vaut exactement http://127.0.0.1:8765/callback.
2. Télécharge le script [atlas_cubrik_oauth_relay.py](https://raw.githubusercontent.com/SachaO/sachaO.github.io/main/telegram_bot/atlas_cubrik_oauth_relay.py) sur ton PC Windows.
3. Ouvre PowerShell dans le dossier du téléchargement.
4. Lance :

```powershell
py .\atlas_cubrik_oauth_relay.py
```

5. Laisse cette fenêtre ouverte.
6. Depuis le même PC, envoie /cubrikconnect à ton bot Telegram.
7. Ouvre le lien OAuth que le bot t'envoie dans le navigateur de ce PC.
8. Accepte uniquement les permissions nécessaires à la lecture du compte, des agents et des historiques. Ne donne pas de permission de trading à ATLAS.
9. Après validation, le navigateur revient sur 127.0.0.1. Le relais transmet le code à Render en HTTPS, puis s'arrête automatiquement.
10. Reviens sur Telegram et demande /cubrik.

## Après la première connexion

Tu n'as pas besoin de lancer le relais à chaque consultation tant que le jeton reste valide. Render conserve les jetons dans PostgreSQL, chiffrés avec CUBRIK_MCP_ENCRYPTION_KEY, et le client MCP utilise un refresh token lorsqu'il est fourni par Cubrik.

Si Cubrik ne fournit pas de refresh token, le jeton d'accès finit par expirer et une nouvelle autorisation manuelle sera nécessaire. Le client ATLAS ne peut pas fabriquer un refresh token lui-même. Pour un renouvellement réellement automatique, le serveur OAuth de Cubrik doit prendre en charge et émettre un refresh token pour cette application MCP (notamment via offline_access si Cubrik l'annonce comme scope pris en charge).

Les commandes ordinaires ne relancent désormais plus toutes seules une demande OAuth. Seule la commande /cubrikconnect ouvre volontairement le parcours de consentement. Si une connexion échoue parce que le jeton n'est plus valable, le bot t'indique d'utiliser cette commande.

## Sécurité

- Le relais écoute uniquement sur 127.0.0.1 et sur le port 8765.
- Il ne stocke aucun jeton ni code OAuth.
- Il transmet seulement le retour OAuth à Render via HTTPS.
- Render vérifie le state OAuth avant de poursuivre.
- ATLAS appelle exclusivement des outils Cubrik de lecture ; aucune permission de trading n'est nécessaire.
- Ne partage jamais un lien OAuth, un code de retour, un jeton ou les variables secrètes Render.