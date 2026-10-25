"""ATLAS balanced multi-signal paper-trading strategy.

All orders are simulated. Signal observations are saved for later evaluation;
this is not yet an online-learning model. Historical prices may be delayed.
"""
import logging
from datetime import datetime, timezone

import yfinance as yf

import concurrent.futures
import os
import requests

AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "").strip()
COUNCIL_ROLES = (
    ("TREND", "Tendance", "Examine les moyennes mobiles, le momentum et la tendance technique."),
    ("RISK", "Risque", "Priorise la préservation du capital, la taille de position et les pertes possibles."),
    ("FUNDAMENTALS", "Fondamentaux", "Évalue les fondamentaux et actualités disponibles, sans inventer de données."),
    ("CONTRARIAN", "Contradicteur", "Cherche les raisons de refuser le signal dominant et teste l'hypothèse opposée."),
    ("MARKET", "Marché et crypto", "Examine le régime de marché, la liquidité et les spécificités des cryptoactifs."),
)


def _council_config(role_key):
    """Resolve one role's API config without logging or exposing any secret."""
    prefix = "ATLAS_AI_" + role_key
    key = os.getenv(prefix + "_API_KEY", "").strip() or AI_API_KEY
    base_url = os.getenv(prefix + "_BASE_URL", "").strip().rstrip("/") or AI_BASE_URL
    model = os.getenv(prefix + "_MODEL", "").strip()
    return key, base_url, model


def council_vote(symbol, name, signal, held):
    """Ask five separately configured model endpoints; failures never authorize orders."""
    configs = {role[0]: _council_config(role[0]) for role in COUNCIL_ROLES}
    missing = []
    for role_key, role_name, _ in COUNCIL_ROLES:
        key, base_url, model = configs[role_key]
        if not key:
            missing.append("ATLAS_AI_" + role_key + "_API_KEY or AI_API_KEY")
        if not base_url:
            missing.append("ATLAS_AI_" + role_key + "_BASE_URL or AI_BASE_URL")
        if not model:
            missing.append("ATLAS_AI_" + role_key + "_MODEL")
    models = [configs[role[0]][2] for role in COUNCIL_ROLES if configs[role[0]][2]]
    if len(models) != len(COUNCIL_ROLES) or len(set(models)) != len(COUNCIL_ROLES):
        missing.append("five distinct ATLAS_AI_<ROLE>_MODEL values")
    if missing:
        log.warning("ATLAS council unavailable for %s: missing/duplicate role configuration %s",
                    symbol, ",".join(missing))
        return {"buy": 0, "sell": 0, "wait": 5, "successful": 0, "available": False}
    context = (
        f"Actif: {name} ({symbol}); cours journalier: {signal['price']:.8g} USD; "
        f"score technique: {signal['score']}/5; MM20: {signal['sma20']:.8g}; "
        f"MM50: {signal['sma50']:.8g}; momentum 5j: {signal['momentum5']:+.2%}; "
        f"momentum 20j: {signal['momentum20']:+.2%}; RSI14: {signal['rsi']:.1f}; "
        f"volatilité quotidienne 20j: {signal['volatility20']:.2%}; position détenue: {held}. "
        "Données journalières potentiellement retardées; aucune actualité ni donnée intrajournalière fournie."
    )
    def ask(role):
        role_key, role_name, focus = role
        api_key, base_url, model = configs[role_key]
        prompt = (
            "Tu votes pour un portefeuille EXCLUSIVEMENT fictif. Ton rôle: " + role_name + ". " + focus +
            "\n" + context + "\nChoisis BUY, SELL ou WAIT. N'invente rien. Si les données sont insuffisantes, WAIT. "
            + ("RÈGLE ABSOLUE : aucune position n'est détenue, donc SELL est interdit ; choisis BUY ou WAIT. " if not held else "Une position est détenue : SELL est autorisé seulement si les éléments le justifient. ")
            + "Réponds en commençant par un seul mot parmi BUY, SELL, WAIT."
        )
        for attempt in (1, 2, 3):
            try:
                response = requests.post(
                    base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                    json={"model": model, "messages": [
                              {"role": "system", "content": "Output exactly one uppercase token: BUY, SELL, or WAIT. No reasoning, no punctuation."},
                              {"role": "user", "content": prompt},
                          ], "temperature": 0.0, "max_tokens": 512},
                    timeout=25,
                )
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    # OpenRouter returns the actionable cause in its JSON error body.
                    # Log only the response body; never log request headers or API keys.
                    log.error(
                        "ATLAS OpenRouter HTTP error: symbol=%s role=%s model=%s status=%s body=%s",
                        symbol, role_name, model, response.status_code,
                        (response.text or "")[:1200],
                    )
                    raise
                payload = response.json()
                choices = payload.get("choices") or []
                choice = choices[0] if choices and isinstance(choices[0], dict) else {}
                message = choice.get("message")
                answer = message.get("content") if isinstance(message, dict) else None
                if isinstance(answer, list):
                    answer = " ".join(
                        part.get("text", "") for part in answer
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
                if not isinstance(answer, str) or not answer.strip():
                    log.warning(
                        "ATLAS council reviewer empty response: symbol=%s role=%s attempt=%s finish_reason=%s",
                        symbol, role_name, attempt, choice.get("finish_reason", "missing"),
                    )
                    if attempt < 3:
                        continue
                    return ("WAIT", False)
                tokens = [token.strip(".,:;!?()[]{}") for token in answer.strip().upper().split()]
                first_word = next((token for token in tokens if token in ("BUY", "SELL", "WAIT")), "")
                if not first_word:
                    log.warning(
                        "ATLAS council reviewer invalid vote: symbol=%s role=%s attempt=%s",
                        symbol, role_name, attempt,
                    )
                    if attempt < 3:
                        continue
                    return ("WAIT", False)
                if first_word == "SELL" and not held:
                    log.warning("ATLAS council rejected SELL vote without a held position: symbol=%s role=%s", symbol, role_name)
                    first_word = "WAIT"
                log.info("ATLAS council vote symbol=%s role=%s vote=%s", symbol, role_name, first_word)
                return (first_word, True)
            except Exception:
                log.exception("ATLAS council reviewer failed: symbol=%s role=%s attempt=%s", symbol, role_name, attempt)
                if attempt == 3:
                    return ("WAIT", False)
        return ("WAIT", False)
    log.info("ATLAS council started symbol=%s model=%s reviewers=%s",
             symbol, AI_MODEL, len(COUNCIL_ROLES))
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(ask, COUNCIL_ROLES))
    votes = [vote for vote, success in outcomes]
    successful = sum(1 for vote, success in outcomes if success)
    result = {"buy": votes.count("BUY"), "sell": votes.count("SELL"),
              "wait": votes.count("WAIT"), "successful": successful,
              "available": successful == len(COUNCIL_ROLES)}
    log.info("ATLAS council summary symbol=%s BUY=%s SELL=%s WAIT=%s",
             symbol, result["buy"], result["sell"], result["wait"])
    return result


from .paper_trading import (
    WATCHLIST, MAX_TRADE_FRACTION, MAX_POSITION_FRACTION,
    snapshot, record_price, record_signal, paper_order, cycle_record,
)
from .discovery import discover_assets

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
    """Get six months of daily history; CoinGecko IDs avoid ambiguous crypto tickers."""
    if symbol.startswith("CG:"):
        coin_id = symbol[3:]
        response = requests.get(
            "https://api.coingecko.com/api/v3/coins/" + coin_id + "/market_chart",
            params={"vs_currency": "usd", "days": "180", "interval": "daily"},
            headers={"User-Agent": "ATLAS-paper-trading/1.0"}, timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        prices = payload.get("prices") or []
        if len(prices) < 60:
            return None
        import pandas as pd
        close = pd.Series(
            [float(point[1]) for point in prices],
            index=pd.to_datetime([int(point[0]) for point in prices], unit="ms", utc=True),
            name="Close",
        ).dropna()
        volumes = payload.get("total_volumes") or []
        volume = pd.Series(
            [float(point[1]) for point in volumes],
            index=pd.to_datetime([int(point[0]) for point in volumes], unit="ms", utc=True),
            name="Volume",
        ).dropna() if volumes else None
        return (close, volume) if len(close) >= 60 else None

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
    """Discover a bounded global candidate universe, then analyse and paper-trade only."""
    updates, decisions, errors = 0, [], []
    universe = dict(WATCHLIST)
    try:
        discovered = discover_assets()
        for asset in discovered:
            symbol = asset.get("symbol", "")
            name = asset.get("name") or symbol
            if symbol and symbol not in universe:
                universe[symbol] = name
        log.info("ATLAS cycle universe: %s total assets (including %s discovered)",
                 len(universe), len([a for a in discovered if a.get("symbol") not in WATCHLIST]))
    except Exception:
        log.exception("ATLAS discovery failed; using configured watchlist only")
    for symbol, name in universe.items():
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
            if wants_sell or wants_buy:
                log.info("ATLAS council trigger symbol=%s reason=%s score=%s held=%s",
                         symbol, "BUY candidate" if wants_buy else "SELL candidate", score, held > 1e-8)
                council = council_vote(symbol, name, signal, held > 1e-8)
            else:
                council = None
                log.info("ATLAS council skipped symbol=%s score=%s held=%s stop_hit=%s reason=no eligible normal BUY/SELL signal",
                         symbol, score, held > 1e-8, stop_hit)

            if held > 1e-8 and (stop_hit or (wants_sell and council and council.get("available") and council["sell"] >= 3)):
                reason = (
                    "Stop de protection atteint (-{:.1f}% depuis le coût moyen) ; ".format(STOP_LOSS_FRACTION * 100)
                    if stop_hit else "Conseil IA : au moins 3/5 votes SELL ; "
                ) + rationale
                paper_order(symbol, "SELL", held, price, reason, FEE_RATE)
                decisions.append("VENTE SIMULÉE {} : {:.6g} à {:.4f} USD — {}".format(symbol, held, price, reason))
            elif wants_buy and council and council.get("available") and council["buy"] >= 3:
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
