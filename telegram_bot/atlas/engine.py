"""ATLAS balanced multi-signal paper-trading strategy.

All orders are simulated. Signal observations are saved for later evaluation;
this is not yet an online-learning model. Historical prices may be delayed.
"""
import logging
from datetime import datetime, timezone

import yfinance as yf

import concurrent.futures
import os
import re
import requests

AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "").strip()
COUNCIL_ROLES = (
    ("Tendance", "Examine les moyennes mobiles et le momentum."),
    ("Risque", "Priorise la préservation du capital et les risques de perte."),
    ("Fondamentaux", "Évalue les éléments fondamentaux disponibles, sans inventer de données."),
    ("Contradicteur", "Cherche les raisons de ne pas suivre le signal dominant."),
    ("Marché et crypto", "Examine le contexte de marché et la forte volatilité crypto."),
)


def council_vote(symbol, name, signal, held):
    """Ask five separately prompted reviewers; unavailable/unclear votes count as WAIT."""
    if not AI_API_KEY or not AI_MODEL:
        return {"buy": 0, "sell": 0, "wait": 5, "available": False}
    context = (
        f"Actif: {name} ({symbol}); cours journalier: {signal['price']:.8g} USD; "
        f"score technique: {signal['score']}/5; MM20: {signal['sma20']:.8g}; "
        f"MM50: {signal['sma50']:.8g}; momentum 5j: {signal['momentum5']:+.2%}; "
        f"momentum 20j: {signal['momentum20']:+.2%}; RSI14: {signal['rsi']:.1f}; "
        f"volatilité quotidienne 20j: {signal['volatility20']:.2%}; position détenue: {held}. "
        "Données journalières potentiellement retardées; aucune actualité ni donnée intrajournalière fournie."
    )
    def ask(role):
        role_name, focus = role
        prompt = (
            "Tu votes pour un portefeuille EXCLUSIVEMENT fictif. Ton rôle: " + role_name + ". " + focus +
            "\n" + context + "\nChoisis BUY, SELL ou WAIT. N'invente rien. Si les données sont insuffisantes, WAIT. "
            "Réponds en commençant par un seul mot parmi BUY, SELL, WAIT."
        )
        try:
            response = requests.post(
                AI_BASE_URL + "/chat/completions",
                headers={"Authorization": "Bearer " + AI_API_KEY, "Content-Type": "application/json"},
                json={"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.2, "max_tokens": 80},
                timeout=12,
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"].strip().upper()
            match = re.match(r"^\s*(BUY|SELL|WAIT)\b", answer)
            return match.group(1) if match else "WAIT"
        except Exception:
            log.exception("ATLAS council reviewer failed: %s", role_name)
            return "WAIT"
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        votes = list(pool.map(ask, COUNCIL_ROLES))
    return {"buy": votes.count("BUY"), "sell": votes.count("SELL"),
            "wait": votes.count("WAIT"), "available": True}


from .paper_trading import (
    WATCHLIST, MAX_TRADE_FRACTION, MAX_POSITION_FRACTION,
    snapshot, record_price, record_signal, paper_order, cycle_record,
)

log = logging.getLogger(__name__)
FEE_RATE = 0.001
MIN_BUY_SCORE = 3
SELL_SCORE = -2
STOP_LOSS_FRACTION = 0.08
MAX_DAILY_VOLATILITY = 0.09


def _series(frame, key):
    value = frame[key]
    if hasattr(value, "columns"):
        value = value.iloc[:, 0]
    return value.dropna()


def get_history(symbol):
    """Get six months of adjusted daily prices, including volume when available."""
    frame = yf.download(
        symbol, period="6mo", interval="1d", auto_adjust=True,
        progress=False, threads=False, timeout=12,
    )
    if frame is None or frame.empty or "Close" not in frame:
        return None
    close = _series(frame, "Close")
    if len(close) < 60:
        return None
    volume = _series(frame, "Volume") if "Volume" in frame else None
    return close, volume


def analyse_signal(close, volume=None):
    """Return a transparent multi-indicator score and a human-readable rationale."""
    price = float(close.iloc[-1])
    sma20 = float(close.tail(20).mean())
    sma50 = float(close.tail(50).mean())
    momentum5 = price / float(close.iloc[-6]) - 1
    momentum20 = price / float(close.iloc[-21]) - 1
    returns = close.pct_change().dropna()
    volatility20 = float(returns.tail(20).std()) if len(returns) >= 20 else 0.0

    delta = close.diff()
    gains = delta.clip(lower=0).rolling(14).mean()
    losses = -delta.clip(upper=0).rolling(14).mean()
    avg_gain = float(gains.iloc[-1]) if len(gains) and gains.iloc[-1] == gains.iloc[-1] else 0.0
    avg_loss = float(losses.iloc[-1]) if len(losses) and losses.iloc[-1] == losses.iloc[-1] else 0.0
    if avg_loss <= 1e-12:
        rsi = 100.0 if avg_gain > 0 else 50.0
    else:
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False).mean()

    votes = []
    votes.append(1 if price > sma20 > sma50 else -1 if price < sma20 < sma50 else 0)
    votes.append(1 if momentum20 > 0.02 else -1 if momentum20 < -0.02 else 0)
    votes.append(1 if momentum5 > 0.005 else -1 if momentum5 < -0.02 else 0)
    votes.append(1 if macd.iloc[-1] > macd_signal.iloc[-1] else -1 if macd.iloc[-1] < macd_signal.iloc[-1] else 0)
    votes.append(1 if 45 <= rsi <= 68 else -1 if rsi >= 75 or rsi < 35 else 0)

    score = sum(votes)
    if volatility20 > MAX_DAILY_VOLATILITY:
        score = min(score, 1)
    positives = sum(v > 0 for v in votes)
    negatives = sum(v < 0 for v in votes)
    rationale = (
        "score {}/5 ; MM20 {:.2f}, MM50 {:.2f}, momentum 5j {:+.2%}, "
        "momentum 20j {:+.2%}, RSI14 {:.1f}, volatilité quotidienne 20j {:.2%}, "
        "{} signaux positifs / {} négatifs"
    ).format(score, sma20, sma50, momentum5, momentum20, rsi, volatility20, positives, negatives)
    return {
        "price": price, "sma20": sma20, "sma50": sma50,
        "momentum5": momentum5, "momentum20": momentum20,
        "rsi": rsi, "volatility20": volatility20, "score": score,
        "positives": positives, "negatives": negatives, "rationale": rationale,
    }


def run_cycle():
    """Refresh configured assets, persist signals and execute simulated orders."""
    updates, decisions, errors = 0, [], []
    for symbol, name in WATCHLIST.items():
        try:
            history = get_history(symbol)
            if history is None:
                errors.append(symbol + ": historique insuffisant ou indisponible")
                continue
            close, volume = history
            signal = analyse_signal(close, volume)
            price = signal["price"]
            if price <= 0:
                errors.append(symbol + ": prix invalide")
                continue
            as_of = str(close.index[-1])
            record_price(symbol, price, as_of)
            record_signal(symbol, signal, as_of)
            updates += 1

            state = snapshot()
            pos = next((p for p in state["positions"] if p["symbol"] == symbol), None)
            held = float(pos["quantity"]) if pos else 0.0
            avg_cost = float(pos["avg_cost"]) if pos else 0.0
            equity = max(float(state["equity"]), 0.0)
            score = signal["score"]
            rationale = signal["rationale"]

            # Hard stop-loss remains deterministic; normal entries/exits need 3/5 AI votes.
            stop_hit = held > 1e-8 and avg_cost > 0 and price <= avg_cost * (1 - STOP_LOSS_FRACTION)
            wants_sell = held > 1e-8 and score <= SELL_SCORE and not stop_hit
            wants_buy = held <= 1e-8 and score >= MIN_BUY_SCORE and state["cash"] > 10
            council = council_vote(symbol, name, signal, held > 1e-8) if (wants_sell or wants_buy) else None

            if held > 1e-8 and (stop_hit or (wants_sell and council and council["sell"] >= 3)):
                reason = (
                    "Stop de protection atteint (-{:.1f}% depuis le coût moyen) ; ".format(STOP_LOSS_FRACTION * 100)
                    if stop_hit else "Conseil IA : au moins 3/5 votes SELL ; "
                ) + rationale
                paper_order(symbol, "SELL", held, price, reason, FEE_RATE)
                decisions.append("VENTE SIMULÉE {} : {:.6g} à {:.4f} USD — {}".format(symbol, held, price, reason))
            elif wants_buy and council and council["buy"] >= 3:
                # Reduce exposure further when volatility is elevated, especially for crypto.
                budget = min(float(state["cash"]) * 0.20, equity * MAX_TRADE_FRACTION)
                if signal["volatility20"] > 0.05:
                    budget *= 0.5
                remaining_capacity = max(0.0, equity * MAX_POSITION_FRACTION)
                budget = min(budget, remaining_capacity)
                qty = round(budget / (price * (1 + FEE_RATE)), 8)
                if qty > 0 and budget >= 10:
                    reason = "Conseil IA : au moins 3/5 votes BUY ; " + rationale
                    try:
                        paper_order(symbol, "BUY", qty, price, reason, FEE_RATE)
                        decisions.append("ACHAT SIMULÉ {} : {:.8g} à {:.4f} USD — {}".format(symbol, qty, price, reason))
                    except ValueError as exc:
                        decisions.append("{} : attente ({}) — {}".format(symbol, exc, rationale))
                else:
                    decisions.append("{} : attente, budget prudent insuffisant — {}".format(symbol, rationale))
            elif council:
                decisions.append("{} : ATTENTE — conseil IA BUY {}/5, SELL {}/5, WAIT {}/5.".format(
                    symbol, council["buy"], council["sell"], council["wait"]))
            else:
                action = "CONSERVATION" if held > 1e-8 else "ATTENTE"
                decisions.append("{} : {} (cours {:.4f} USD ; {})".format(symbol, action, price, rationale))
        except Exception as exc:
            log.exception("ATLAS cycle failed for %s", symbol)
            errors.append("{}: {}".format(symbol, str(exc)[:160]))

    summary = "Cours actualisés: {}. Décisions: {}. Erreurs: {}.".format(updates, len(decisions), len(errors))
    cycle_record("ok" if updates and not errors else "partial" if updates else "error", summary)
    return {
        "updated": updates, "decisions": decisions, "errors": errors,
        "as_of": datetime.now(timezone.utc).isoformat(), "portfolio": snapshot(),
    }
