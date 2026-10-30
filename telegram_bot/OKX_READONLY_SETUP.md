# ATLAS — connexion OKX en lecture seule

## Ce que fait l'intégration

ATLAS peut consulter, sans rien modifier sur OKX :
- la valeur totale du compte de trading et les fonds disponibles ;
- les actifs du compte de trading et du compte de financement ;
- les positions ouvertes, leur taille, prix d'entrée, prix de marché, P&L latent, levier et prix de liquidation lorsqu'OKX les fournit ;
- les ordres en attente ;
- les dernières exécutions et les ordres terminés ;
- l'historique étendu des exécutions et ordres (jusqu'à trois mois selon les API OKX) ;
- les mouvements du compte et frais sur les sept derniers jours ;
- le mode de compte et le mode de position, lorsqu'ils sont disponibles.

Les historiques sont bornés par les fenêtres et la pagination de l'API OKX. Cette intégration ne constitue pas un relevé fiscal ni un accès exhaustif aux produits Earn ou à d'autres comptes externes.

## Sécurité obligatoire

Créer une clé API distincte pour ATLAS, par exemple ATLAS-READ-ONLY.

Dans OKX, choisir :
- autorisation Read uniquement ;
- ne pas activer Trade ;
- ne pas activer Withdraw ;
- ne pas réutiliser la clé de trading de Cubrik ;
- conserver séparément la clé, le secret et la passphrase ;
- lier la clé à des IP autorisées si une adresse IP de sortie fixe est disponible pour le service Render.

Le code ATLAS utilise exclusivement une liste fermée d'URL autorisées en GET. Il ne contient aucune fonction HTTP de placement, modification ou annulation d'ordre, transfert, réglage du levier ou retrait.

## Variables à ajouter dans Render

Dans le service Render qui héberge le bot, ouvrir Environment et ajouter :

```env
OKX_READ_API_KEY=<clé API Read>
OKX_READ_API_SECRET=<secret API>
OKX_READ_PASSPHRASE=<passphrase de la clé>
OKX_API_BASE_URL=https://eea.okx.com
OKX_READ_SIMULATED=0
TELEGRAM_CHAT_ID=<identifiant de ta conversation privée Telegram>
```

Les trois premières valeurs sont secrètes. Les saisir directement dans Render, jamais dans GitHub, un message Telegram ou une conversation ChatGPT.

Pour un compte OKX de démonstration, utiliser une clé API de démonstration et OKX_READ_SIMULATED=1. Pour un compte réel, utiliser 0. Le domaine par défaut eea.okx.com correspond aux comptes EEE ; ne le changer que si le compte OKX indique un autre domaine régional.

TELEGRAM_CHAT_ID limite ces commandes au propriétaire du compte. Le récupérer avec /chatid dans le bot puis le saisir dans Render.

## Commandes Telegram

- /okx : tableau de bord du compte (soldes, actifs, positions, ordres en attente, dernières exécutions).
- /okxhistorique : historique des exécutions et ordres, ainsi que les mouvements et frais récents.
- /atlas, /positions, /trades : restent le portefeuille fictif de simulation ; ils ne sont pas fusionnés avec le compte OKX réel.
- /atlastest : diagnostic des cinq avis IA de simulation ; ne passe aucun ordre.

## Déploiement et validation

Après fusion de la branche et déploiement Render :
1. ajouter les variables ci-dessus ;
2. vérifier les logs de démarrage et /status ;
3. lancer /okx puis /okxhistorique depuis la conversation Telegram autorisée ;
4. comparer les soldes et positions avec l'interface OKX ;
5. vérifier que les erreurs partielles sont clairement indiquées si un endpoint n'est pas disponible.

L'intégration n'est pas considérée comme validée tant que les données renvoyées n'ont pas été comparées à l'interface OKX.

## Limites

Le connecteur affiche uniquement les informations auxquelles les endpoints de lecture de la clé donnent accès. Les historiques OKX ont des fenêtres de conservation et des limites de pagination. Les produits Earn, les comptes de sous-comptes, ou des produits non exposés par les endpoints utilisés peuvent nécessiter des endpoints ou une clé distincts. Aucun ordre n'est envoyé par cette intégration.
