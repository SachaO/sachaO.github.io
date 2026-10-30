#!/usr/bin/env python3
"""Temporary localhost relay for ATLAS <-> Cubrik native OAuth.

Run this on the same Windows PC where the browser will complete Cubrik login.
It listens only on 127.0.0.1:8765, forwards the OAuth response to the
Render callback over HTTPS, then exits. It never stores OAuth tokens.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = 8765
RENDER_CALLBACK = "https://sachao-github-io.onrender.com/cubrik/callback"


class CallbackHandler(BaseHTTPRequestHandler):
    server_version = "ATLAS-Cubrik-Relay/1.0"

    def log_message(self, fmt, *args):
        # Never log request paths: the browser URL contains a one-time OAuth code.
        return

    def _html(self, status, title, message):
        body = (
            "<!doctype html><html lang='fr'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>ATLAS – Cubrik</title><body style='font:16px system-ui;max-width:680px;"
            "margin:48px auto;padding:0 20px;background:#111;color:#eee'>"
            "<h2>" + title + "</h2><p>" + message + "</p>"
            "<p>Tu peux fermer cet onglet et revenir sur Telegram.</p></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self._html(404, "Adresse inconnue", "Ce relais attend uniquement le retour OAuth de Cubrik.")
            return

        params = parse_qs(parsed.query, keep_blank_values=True)
        payload = {}
        for key in ("code", "state", "iss", "error", "error_description"):
            values = params.get(key)
            if values:
                payload[key] = values[0]

        if not payload.get("state"):
            self._html(400, "Retour OAuth incomplet", "Le paramètre de sécurité state est absent. Relance /cubrikconnect.")
            return
        if not payload.get("code") and not payload.get("error"):
            self._html(400, "Retour OAuth incomplet", "Cubrik n'a renvoyé ni code ni erreur. Relance /cubrikconnect.")
            return

        request = urllib.request.Request(
            RENDER_CALLBACK,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/html"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = response.read(1200).decode("utf-8", errors="replace")
                status = response.status
            if status == 200:
                self._html(200, "Autorisation transmise à ATLAS", "Le code de retour a été relayé à Render. ATLAS termine maintenant la connexion à Cubrik.")
            else:
                self._html(502, "Réponse Render inattendue", "Render a répondu avec le statut " + str(status) + ". Vérifie les logs et relance /cubrikconnect.")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(800).decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            print("Render rejected OAuth callback, HTTP", exc.code, detail[:250])
            self._html(502, "Render n'a pas accepté le retour", "Le serveur ATLAS a refusé le retour OAuth (HTTP " + str(exc.code) + "). Vérifie que /cubrikconnect est toujours en attente.")
            return
        except Exception as exc:
            print("Unable to forward OAuth callback:", type(exc).__name__)
            self._html(502, "Impossible de joindre Render", "Le retour n'a pas pu être transmis. Vérifie ta connexion puis relance /cubrikconnect.")
            return

        # Stop the one-shot local listener after forwarding this callback.
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main():
    try:
        server = ThreadingHTTPServer((HOST, PORT), CallbackHandler)
    except OSError as exc:
        print("Impossible d'ouvrir http://127.0.0.1:8765 :", exc)
        print("Ferme toute application qui utilise le port 8765, puis relance ce script.")
        raise SystemExit(1)

    print("Relais ATLAS/Cubrik prêt.")
    print("Il écoute uniquement sur ton PC : http://127.0.0.1:8765/callback")
    print("Laisse cette fenêtre ouverte, puis envoie /cubrikconnect sur Telegram.")
    print("Le relais s'arrêtera automatiquement après le retour OAuth.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nRelais arrêté.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
