"""ATLAS balanced multi-signal paper-trading strategy.

All orders are simulated. Signal observations are saved for later evaluation;
this is not yet an online-learning model. Historical prices may be delayed.
"""
import logging
from datetime import datetime, timezone

import yfinance as yf

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

            # The stop is checked against the latest available daily close, not intraday ticks.
            stop_hit = held > 1e-8 and avg_cost > 0 and price <= avg_cost * (1 - STOP_LOSS_FRACTION)
            if held > 1e-8 and (stop_hit or score <= SELL_SCORE):
                reason = (
                    "Stop de protection atteint (-{:.1f}% depuis le coût moyen) ; ".format(STOP_LOSS_FRACTION * 100)
                    if stop_hit else "Signaux techniques devenus majoritairement négatifs ; "
                ) + rationale
                paper_order(symbol, "SELL", held, price, reason, FEE_RATE)
                decisions.append("VENTE SIMULÉE {} : {:.6g} à {:.4f} USD — {}".format(symbol, held, price, reason))
            elif held <= 1e-8 and score >= MIN_BUY_SCORE and state["cash"] > 10:
                # Crypto remains subject to smaller budgets when daily volatility is elevated.
                budget = min(float(state["cash"]) * 0.20, equity * MAX_TRADE_FRACTION)
                if signal["volatility20"] > 0.05:
                    budget *= 0.5
                remaining_capacity = max(0.0, equity * MAX_POSITION_FRACTION)
                budget = min(budget, remaining_capacity)
                qty = round(budget / (price * (1 + FEE_RATE)), 8)
                if qty > 0 and budget >= 10:
                    reason = "Consensus d'au moins 3 signaux sur 5 ; " + rationale
                    try:
                        paper_order(symbol, "BUY", qty, price, reason, FEE_RATE)
                        decisions.append("ACHAT SIMULÉ {} : {:.8g} à {:.4f} USD — {}".format(symbol, qty, price, reason))
                    except ValueError as exc:
                        decisions.append("{} : attente ({}) — {}".format(symbol, exc, rationale))
                else:
                    decisions.append("{} : attente, budget prudent insuffisant — {}".format(symbol, rationale))
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
