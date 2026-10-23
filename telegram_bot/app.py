import os
import logging
import html
from urllib.parse import urlencode
from datetime import datetime, timezone

import feedparser
import requests
from flask import Flask, request, abort

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"].strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
_setup_done = False
RSS_TIMEOUT = 12
MAX_NEWS_ITEMS = 5


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
        {"command": "help", "description": "Afficher cette aide"},
        {"command": "news", "description": "Actualités financières via RSS"},
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


def search_rss_news(topic=""):
    """Fetch current financial headlines from Google News RSS search results."""
    topic = topic.strip()
    if topic:
        query = f"{topic} finance stock market"
    else:
        query = (
            '"stock market" OR stocks OR earnings OR inflation OR '
            '"central bank" OR economy OR investing'
        )
    params = urlencode({
        "q": query,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })
    feed_url = f"https://news.google.com/rss/search?{params}"
    response = requests.get(
        feed_url,
        headers={"User-Agent": "TRADING_AI_ALERTES/1.0 (financial news RSS reader)"},
        timeout=RSS_TIMEOUT,
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    articles = []
    seen_links = set()
    for entry in feed.entries:
        link = entry.get("link", "").strip()
        title = html.unescape(entry.get("title", "")).strip()
        if not link or not title or link in seen_links:
            continue
        seen_links.add(link)
        source = entry.get("source", {}).get("title", "Google News")
        published = entry.get("published", "")
        articles.append({
            "title": title,
            "link": link,
            "source": source,
            "published": published,
        })
        if len(articles) >= MAX_NEWS_ITEMS:
            break
    return articles


def format_news_message(topic=""):
    try:
        articles = search_rss_news(topic)
    except (requests.RequestException, Exception):
        logging.exception("Unable to fetch financial RSS news")
        return "⚠️ Impossible de récupérer les actualités pour le moment. Réessaie dans quelques minutes."

    heading = "📰 Actualités financières"
    if topic:
        heading += f" — {topic}"
    heading += "\n\n"

    if not articles:
        return heading + "Aucun article trouvé pour cette recherche."

    lines = [heading.rstrip()]
    lines.append("Titres récupérés via Google News RSS. Ouvre les liens pour lire les articles originaux.\n")
    for index, article in enumerate(articles, start=1):
        lines.append(f"{index}. {article['title']}\nSource : {article['source']}\n{article['link']}")
    lines.append("\nℹ️ Ce sont des titres d'actualité, pas des recommandations d'achat ou de vente.")
    return "\n\n".join(lines)


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
    parts = text.split()
    command = parts[0].split("@")[0].lower() if parts else ""
    topic = " ".join(parts[1:]).strip()
    logging.info("Telegram message received; command=%s", command or "(no text)")

    if command == "/start":
        reply = (
            "Bienvenue sur TRADING_AI_ALERTES 📈\n\n"
            "Ce bot est dédié à l'argent, aux marchés financiers et aux actions en Bourse.\n"
            "Utilise /news pour rechercher les dernières actualités financières via RSS.\n"
            "La collecte planifiée et l'analyse IA seront ajoutées ensuite."
        )
    elif command == "/help":
        reply = (
            "Commandes disponibles :\n"
            "/start — présentation du bot\n"
            "/help — afficher cette aide\n"
            "/news — actualités financières récentes\n"
            "/news ORCL — rechercher les nouvelles d'une entreprise ou d'un sujet\n"
            "/status — vérifier que le bot répond\n\n"
            "Les titres proviennent d'une recherche Google News RSS. Ils ne constituent pas des recommandations d'investissement."
        )
    elif command == "/news":
        reply = format_news_message(topic)
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
