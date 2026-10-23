# TRADING_AI_ALERTES — bot Telegram

Ce service traite les commandes Telegram via un webhook. GitHub Pages ne peut pas exécuter ce serveur : il faut le déployer sur un hébergeur web qui accepte une application Python (par exemple Render).

## Variables d'environnement à configurer chez l'hébergeur

- `TELEGRAM_BOT_TOKEN` : le token du bot, uniquement dans les variables secrètes de l'hébergeur.
- `TELEGRAM_WEBHOOK_SECRET` : une chaîne aléatoire secrète, entre 1 et 256 caractères, composée de lettres, chiffres, tirets et underscores.
- `SITE_URL` (facultatif) : URL de la page de veille boursière.

Ne placez jamais le token dans les fichiers du dépôt.

## Déploiement

1. Créer un service web Python depuis ce dépôt et définir le répertoire racine sur `telegram_bot`.
2. Commande d'installation : `pip install -r requirements.txt`
3. Commande de démarrage : `gunicorn app:app`
4. Ajouter les variables secrètes ci-dessus.
5. Une fois le service déployé en HTTPS, configurer le webhook Telegram sur `https://VOTRE-DOMAINE/telegram-webhook`, avec le même secret dans l'en-tête `secret_token`.

## Commandes prises en charge

- `/start`, `/help`, `/portfolio`, `/news`, `/status`

Cette première version répond aux commandes. La récupération automatique et l'analyse des actualités boursières doivent être intégrées séparément; `/news` renvoie pour le moment vers la page de veille.
