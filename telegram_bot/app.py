import os
import logging
from flask import Flask, request, abort
import requests

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"].strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
_setup_done = False


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
        {"command": "news", "description": "Actualités financières et boursières"},
        {"command": "status", "description": "Vérifier le bot"},
    ]
    response = requests.post(f"{API}/setMyCommands", json={"commands": commands}, timeout=15)
    response.raise_for_status()
    logging.info("Telegram bot commands configured successfully.")


def configure_telegram():
    global _setup_done
    if _setup_done:
        return
    if not PUBLIC_URL:
        logging.warning("PUBLIC_URL is not configured; Telegram webhook setup skipped.")
        return
    webhook_url = f"{PUBLIC_URL}/telegram-webhook"
    logging.info("Configuring Telegram webhook for public service URL: %s", PUBLIC_URL)
    set_commands()
    response = requests.post(
        f"{API}/setWebhook",
        json={
            "url": webhook_url,
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message"],
        },
        timeout=15,
    )
    if not response.ok:
        logging.error("Telegram setWebhook rejected request (HTTP %s): %s", response.status_code, response.text)
        response.raise_for_status()
    logging.info("Telegram webhook configured successfully.")
    _setup_done = True


@app.before_request
def ensure_telegram_configured():
    configure_telegram()


@app.get("/")
def health():
    return "Trading alerts bot is running.", 200


@app.post("/telegram-webhook")
def telegram_webhook():
    logging.info("Incoming Telegram webhook request received.")
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        logging.warning("Telegram webhook rejected: secret token header did not match.")
        abort(403)

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message or not message.get("chat"):
        logging.info("Telegram webhook received an update without a chat message.")
        return "ok", 200

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    command = text.split()[0].split("@")[0].lower() if text else ""
    logging.info("Telegram message received; command=%s", command or "(no text)")

    if command == "/start":
        reply = (
            "Bienvenue sur TRADING_AI_ALERTES 📈\n\n"
            "Ce bot est dédié à l'argent, aux marchés financiers et aux actions en Bourse.\n"
            "Utilise /help pour découvrir les commandes.\n"
            "Les actualités boursières automatiques seront ajoutées dans une prochaine étape."
        )
    elif command == "/help":
        reply = (
            "Commandes disponibles :\n"
            "/start — présentation du bot\n"
            "/help — afficher cette aide\n"
            "/news — actualités financières et boursières\n"
            "/status — vérifier que le bot répond\n\n"
            "Ce bot est indépendant de tout portfolio ou site scolaire."
        )
    elif command == "/news":
        reply = (
            "📰 La rubrique actualités financières est prévue.\n"
            "La récupération automatique des nouvelles de marché n'est pas encore configurée. "
            "Aucun site de portfolio scolaire n'est lié à cette commande."
        )
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
