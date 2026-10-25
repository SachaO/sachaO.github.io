# ATLAS — configuration du conseil IA (simulation uniquement)

## Comportement déjà présent dans le code

- Le workflow GitHub Actions lance un cycle à chaque heure (cron `23 * * * *`).
- Les données et indicateurs sont calculés pour toute la watchlist, mais le conseil des cinq IA n'est appelé que si un actif est candidat à l'achat (score >= 3, sans position) ou à la vente (score <= -2, avec position).
- Les ordres restent exclusivement fictifs via `paper_order()`. Le stop-loss de protection reste déterministe.
- Si la configuration du conseil est incomplète, les votes ne sont pas considérés comme disponibles : aucun achat/vente normal n'est autorisé par le conseil.
- Les cours utilisés pour le cycle viennent des clôtures quotidiennes Yahoo Finance ; ce n'est pas un flux garanti en temps réel.

## Architecture proposée

Les rôles sont distincts par consigne et modèle. Cette proposition utilise deux fournisseurs pour réduire la dépendance à un seul service :

| Rôle | Fournisseur | Identifiant de modèle | Endpoint |
|---|---|---|---|
| Tendance | Groq | `openai/gpt-oss-20b` | `https://api.groq.com/openai/v1` |
| Risque | Groq | `openai/gpt-oss-120b` | `https://api.groq.com/openai/v1` |
| Fondamentaux | OpenRouter | `deepseek/deepseek-chat-v3.1` | `https://openrouter.ai/api/v1` |
| Contradicteur | OpenRouter | `z-ai/glm-5.2` | `https://openrouter.ai/api/v1` |
| Marché et crypto | OpenRouter | `deepseek/deepseek-v4-flash-vision-exp` | `https://openrouter.ai/api/v1` |

Les identifiants de modèles et les tarifs évoluent : vérifier leur disponibilité dans les catalogues des fournisseurs avant activation. Les cinq modèles doivent être distincts. L'utilisation de plusieurs modèles n'implique pas que leurs avis soient statistiquement indépendants ni qu'ils soient rentables.

## Variables Render à ajouter manuellement

Aucune clé n'est enregistrée dans ce fichier. Ajouter les valeurs non secrètes ci-dessous dans Render > Environment. Pour les clés API, utiliser les champs secrets de Render et ne jamais les placer dans GitHub, dans ce fichier ou dans le chat.

```env
ATLAS_AI_TREND_MODEL=openai/gpt-oss-20b
ATLAS_AI_TREND_BASE_URL=https://api.groq.com/openai/v1
ATLAS_AI_TREND_API_KEY=<clé Groq à ajouter dans Render>

ATLAS_AI_RISK_MODEL=openai/gpt-oss-120b
ATLAS_AI_RISK_BASE_URL=https://api.groq.com/openai/v1
ATLAS_AI_RISK_API_KEY=<clé Groq à ajouter dans Render>

ATLAS_AI_FUNDAMENTALS_MODEL=deepseek/deepseek-chat-v3.1
ATLAS_AI_FUNDAMENTALS_BASE_URL=https://openrouter.ai/api/v1
ATLAS_AI_FUNDAMENTALS_API_KEY=<clé OpenRouter à ajouter dans Render>

ATLAS_AI_CONTRARIAN_MODEL=z-ai/glm-5.2
ATLAS_AI_CONTRARIAN_BASE_URL=https://openrouter.ai/api/v1
ATLAS_AI_CONTRARIAN_API_KEY=<clé OpenRouter à ajouter dans Render>

ATLAS_AI_MARKET_MODEL=deepseek/deepseek-v4-flash-vision-exp
ATLAS_AI_MARKET_BASE_URL=https://openrouter.ai/api/v1
ATLAS_AI_MARKET_API_KEY=<clé OpenRouter à ajouter dans Render>
```

Les clés spécifiques par rôle peuvent être remplacées par `AI_API_KEY` si elles utilisent toutes le même fournisseur ; ce n'est pas le cas du mélange Groq/OpenRouter proposé. Ne pas modifier les variables déjà présentes ni toucher à `DATABASE_URL), au capital ou aux positions simulées.

## Budget et maîtrise des coûts

- Budget de départ suggéré : 15–25 € par mois pour l'API, avec suivi réel de la consommation.
- Les cinq IA ne tournent pas sur chaque actif à chaque heure : elles sont sollicitées uniquement pour les signaux candidats. Le coût dépend donc du nombre de candidats, de la taille des prompts et des réponses.
- Ajouter des plafonds de dépenses côté fournisseur lorsqu'ils sont disponibles, des alertes de facturation et des limites de crédit. Une limite d'API fournisseur est préférable à un simple plafond codé dans le bot.
- Les modèles gratuits sont utiles pour tester, mais les quotas et la disponibilité peuvent être insuffisants pour une tâche horaire.

## Procédure d'activation sûre

1. Ajouter les variables dans Render sans supprimer les variables existantes.
2. Vérifier que chaque modèle est disponible avec le compte fournisseur correspondant.
3. Redéployer puis lancer `/atlastest` : ce test doit demander cinq votes sans exécuter d'ordre.
4. Examiner les logs pour confirmer cinq réponses valides et les erreurs de fournisseur, sans afficher de clés.
5. Observer plusieurs jours de cycles simulés, puis évaluer les résultats à 1, 3 et 7 jours avant toute modification de stratégie.

Cette configuration ne passe aucun ordre réel et ne garantit aucun rendement.
