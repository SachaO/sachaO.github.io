import os
import logging
from flask import Flask, request, abort
import requests

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"].strip()
SITE_URL = os.environ.get("SITE_URL", "https://sachao.github.io/sachaO.github.io/bourse.html").strip()
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send_message(chat_id, text):
    response = requests.post(
        f"{API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=15,
    )
    response.raise_for_status()


def set_commands():
    commands = [
        {"command": "start", "description": "Démarrer le bot"},
        {"command": "help", "description": "Afficher les commandes"},
        {"command": "portfolio", "description": "Voir les valeurs suivies"},
        {"command": "news", "description": "Ouvrir la veille boursière"},
        {"command": "status", "description": "Vérifier le bot"},
    ]
    response = requests.post(f"{API}/setMyCommands", json={"commands": commands}, timeout=15)
    response.raise_for_status()


@app.get("/")
def health():
    return "Trading alerts bot is running.", 200


@app.post("/telegram-webhook")
def telegram_webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message or not message.get("chat"):
        return "ok", 200

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    command = text.split()[0].split("@")[0].lower() if text else ""

    if command == "/start":
        reply = (
            "Bienvenue sur TRADING_AI_ALERTES 📈\n\n"
            "Je suis connecté. Utilise /help pour voir les commandes disponibles.\n"
            "Les alertes automatiques d'actualités seront ajoutées dans une prochaine étape."
        )
    elif command == "/help":
        reply = (
            "Commandes disponibles :\n"
            "/start — démarrer le bot\n"
            "/help — afficher cette aide\n"
            "/portfolio — valeurs suivies\n"
            "/news — ouvrir la veille boursière\n"
            "/status — vérifier que le bot répond"
        )
    elif command == "/portfolio":
        reply = (
            "📊 Valeurs suivies dans la veille :\n"
            "• Oracle (ORCL)\n"
            "• Innventure (INV)\n"
            "• SpaceX (entreprise non cotée)\n"
            "• Sionna Therapeutics (SION)\n"
            "• Scholar Rock (SRRK)\n\n"
            "Cette liste correspond aux valeurs surveillées, pas à une confirmation de tes positions actuelles."
        )
    elif command == "/news":
        reply = f"📰 Consulte la veille boursière ici : {SITE_URL}\n\nLes actualités ne sont pas encore récupérées automatiquement par cette commande."
    elif command == "/status":
        reply = "✅ Le bot Telegram fonctionne et répond aux commandes."
    else:
        reply = "Je n'ai pas reconnu cette commande. Envoie /help pour voir les commandes disponibles."

    try:
        send_message(chat_id, reply)
    except requests.RequestException:
        logging.exception("Échec de l'envoi d'une réponse Telegram")
        return "telegram send failed", 500
    return "ok", 200


if __name__ == "__main__":
    set_commands()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
