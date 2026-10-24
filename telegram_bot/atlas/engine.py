"""ATLAS baseline strategy: daily paper-trading cycle using public Yahoo Finance data."""
import logging
from datetime import datetime, timezone
import yfinance as yf
from .paper_trading import WATCHLIST, MAX_TRADE_FRACTION, connect, snapshot, record_price, paper_order, cycle_record

log = logging.getLogger(__name__)
FEE_RATE = 0.001


def get_history(symbol):
    frame = yf.download(symbol, period="6mo", interval="1d", auto_adjust=True, progress=False, threads=False)
    if frame is None or frame.empty or "Close" not in frame:
        return None
    close = frame["Close"]
    # yfinance may return a one-column DataFrame for recent versions.
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close = close.dropna()
    return close if len(close) >= 50 else None


def run_cycle():
    """Refresh quotes and apply a deliberately simple, explainable strategy."""
    updates, decisions, errors = 0, [], []
    for symbol, name in WATCHLIST.items():
        try:
            close = get_history(symbol)
            if close is None:
                errors.append(symbol + ": historique insuffisant")
                continue
            price = float(close.iloc[-1])
            if price <= 0:
                errors.append(symbol + ": prix invalide")
                continue
            as_of = str(close.index[-1])
            record_price(symbol, price, as_of)
            updates += 1

            sma20 = float(close.tail(20).mean())
            sma50 = float(close.tail(50).mean())
            prior = float(close.iloc[-6])
            momentum5 = price / prior - 1 if prior else 0
            state = snapshot()
            pos = next((p for p in state["positions"] if p["symbol"] == symbol), None)
            held = float(pos["quantity"]) if pos else 0.0
            equity = state["equity"]
            # Conservative baseline: require trend and positive short momentum to buy;
            # exit when trend weakens or momentum turns sharply negative.
            if held <= 1e-8 and price > sma20 > sma50 and momentum5 > 0.01 and state["cash"] > 10:
                budget = min(state["cash"] * 0.20, equity * MAX_TRADE_FRACTION)
                qty = round(budget / (price * (1 + FEE_RATE)), 6)
                if qty > 0:
                    reason = "Tendance haussière (cours > moyenne 20j > moyenne 50j) et momentum 5j positif"
                    try:
                        paper_order(symbol, "BUY", qty, price, reason, FEE_RATE)
                        decisions.append("ACHAT SIMULÉ {} : {:.6g} à {:.2f} — {}".format(symbol, qty, price, reason))
                    except ValueError as exc:
                        decisions.append("{} : pas d'achat ({})".format(symbol, exc))
            elif held > 1e-8 and (price < sma20 or momentum5 < -0.04):
                reason = "Sortie de risque : cours sous moyenne 20j ou momentum 5j inférieur à -4 %"
                paper_order(symbol, "SELL", held, price, reason, FEE_RATE)
                decisions.append("VENTE SIMULÉE {} : {:.6g} à {:.2f} — {}".format(symbol, held, price, reason))
            else:
                decisions.append("{} : conservation / attente (cours {:.2f}, MM20 {:.2f}, MM50 {:.2f}, momentum 5j {:+.2%})".format(symbol, price, sma20, sma50, momentum5))
        except Exception as exc:
            log.exception("ATLAS cycle failed for %s", symbol)
            errors.append("{}: {}".format(symbol, str(exc)[:160]))

    summary = "Cours actualisés: {}. Décisions: {}. Erreurs: {}.".format(updates, len(decisions), len(errors))
    cycle_record("ok" if updates and not errors else "partial" if updates else "error", summary)
    return {"updated": updates, "decisions": decisions, "errors": errors, "as_of": datetime.now(timezone.utc).isoformat(), "portfolio": snapshot()}
