# ATLAS — portefeuille fictif

ATLAS est un moteur de simulation uniquement. Il ne se connecte à aucun courtier et ne peut pas envoyer d'ordre réel.

## Première version

- Capital fictif initial : 10 000 € (variable avec `ATLAS_STARTING_CASH`).
- Cours et historique journalier récupérés via `yfinance` pour les actions/ETF et les cryptomonnaies prises en charge par Yahoo Finance.
- Stratégie de base explicable : tendance sur moyennes mobiles 20/50 jours et momentum sur 5 jours.
- Frais simulés de 0,1 % par ordre ; taille des positions plafonnée.
- Registre SQLite des cours, positions, opérations et cycles.
- Commandes Telegram : `/atlas`, `/positions`, `/trades`.
- Endpoint privé `POST /atlas-cycle`, protégé par le même secret que les alertes planifiées, appelé toutes les six heures par GitHub Actions.

## Persistance obligatoire

Par défaut, la base est écrite dans `/tmp/atlas.sqlite3`. Sur un hébergement dont le disque est éphémère, les données peuvent disparaître lors d'un redémarrage ou d'un déploiement. Pour conserver réellement le portefeuille, configure un disque persistant Render et définis `ATLAS_DB_PATH` sur un chemin de ce disque (par exemple `/var/data/atlas.sqlite3`). Vérifie ce point avant de considérer l'historique comme fiable.

## Limites à connaître

Cette version est une base de départ, pas encore un système d'apprentissage validé. Elle enregistre les résultats mais ne modifie pas seule sa stratégie. Les cours peuvent être retardés, indisponibles ou incomplets ; les cycles échoués sont journalisés. Les signaux techniques simples ne garantissent pas de bénéfices. Il faudra ensuite ajouter les contrôles de qualité des données, les métriques de performance et de drawdown, un benchmark, et une évaluation hors échantillon avant de comparer des variantes de stratégie.
