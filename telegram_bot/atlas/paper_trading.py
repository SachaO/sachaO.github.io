"""ATLAS paper-trading ledger backed by PostgreSQL (Neon). Simulated orders only."""
import os
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
STARTING_CASH = float(os.getenv("ATLAS_STARTING_CASH", "10000"))
WATCHLIST = {
    "ORCL": "Oracle", "NVDA": "NVIDIA", "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq-100 ETF", "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum",
    "SOL-USD": "Solana", "XRP-USD": "XRP",
}
MAX_POSITION_FRACTION = 0.15
MAX_TRADE_FRACTION = 0.05


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL n'est pas configurée sur Render.")
    db = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    with db.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS atlas_account (
            id INTEGER PRIMARY KEY CHECK (id=1),
            cash DOUBLE PRECISION NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS atlas_positions (
            symbol TEXT PRIMARY KEY,
            quantity DOUBLE PRECISION NOT NULL,
            avg_cost DOUBLE PRECISION NOT NULL
        );
        CREATE TABLE IF NOT EXISTS atlas_prices (
            symbol TEXT PRIMARY KEY,
            price DOUBLE PRECISION NOT NULL,
            as_of TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS atlas_trades (
            id BIGSERIAL PRIMARY KEY,
            at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity DOUBLE PRECISION NOT NULL,
            price DOUBLE PRECISION NOT NULL,
            fee DOUBLE PRECISION NOT NULL,
            reason TEXT NOT NULL,
            strategy TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS atlas_cycles (
            id BIGSERIAL PRIMARY KEY,
            at TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS atlas_signals (
            id BIGSERIAL PRIMARY KEY,
            at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            price DOUBLE PRECISION NOT NULL,
            score INTEGER NOT NULL,
            momentum5 DOUBLE PRECISION NOT NULL,
            momentum20 DOUBLE PRECISION NOT NULL,
            rsi DOUBLE PRECISION NOT NULL,
            volatility20 DOUBLE PRECISION NOT NULL
        );
        CREATE INDEX IF NOT EXISTS atlas_signals_symbol_at_idx ON atlas_signals(symbol, at);
        """)
        cur.execute(
            "INSERT INTO atlas_account(id,cash,created_at) VALUES(1,%s,%s) ON CONFLICT(id) DO NOTHING",
            (STARTING_CASH, now()),
        )
    db.commit()
    return db


def snapshot():
    db = connect()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT cash,created_at FROM atlas_account WHERE id=1")
            account = cur.fetchone()
            cur.execute("SELECT symbol,quantity,avg_cost FROM atlas_positions WHERE quantity > 0.00000001")
            raw_positions = cur.fetchall()
            positions = []
            value = float(account["cash"])
            for p in raw_positions:
                cur.execute("SELECT price,as_of FROM atlas_prices WHERE symbol=%s", (p["symbol"],))
                price = cur.fetchone()
                current = float(price["price"]) if price else float(p["avg_cost"])
                market_value = p["quantity"] * current
                value += market_value
                positions.append({
                    "symbol":p["symbol"],"name":WATCHLIST.get(p["symbol"],p["symbol"]),
                    "quantity":p["quantity"],"avg_cost":p["avg_cost"],"price":current,
                    "market_value":market_value,"pnl":market_value-p["quantity"]*p["avg_cost"],
                    "as_of":price["as_of"] if price else "prix indisponible"
                })
            cur.execute("SELECT id,at,symbol,side,quantity,price,fee,reason,strategy FROM atlas_trades ORDER BY id DESC LIMIT 10")
            trades = cur.fetchall()
        return {
            "cash":float(account["cash"]),"equity":value,"pnl":value-STARTING_CASH,
            "return_pct":(value/STARTING_CASH-1)*100 if STARTING_CASH else 0,
            "positions":positions,"trades":trades,"starting_cash":STARTING_CASH,
        }
    finally:
        db.close()


def record_price(symbol, price, as_of=None):
    if price <= 0:
        raise ValueError("Prix invalide")
    db=connect()
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO atlas_prices(symbol,price,as_of) VALUES(%s,%s,%s) ON CONFLICT(symbol) DO UPDATE SET price=EXCLUDED.price,as_of=EXCLUDED.as_of",
                (symbol,float(price),as_of or now()),
            )
        db.commit()
    finally:
        db.close()


def record_signal(symbol, signal):
    """Persist signals so later evaluation can measure which signals worked."""
    db=connect()
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO atlas_signals(at,symbol,price,score,momentum5,momentum20,rsi,volatility20) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (now(), symbol, float(signal["price"]), int(signal["score"]),
                 float(signal["momentum5"]), float(signal["momentum20"]),
                 float(signal["rsi"]), float(signal["volatility20"])),
            )
        db.commit()
    finally:
        db.close()


def paper_order(symbol, side, quantity, price, reason, fee_rate=0.001):
    """Execute a simulated order with cash and position checks."""
    side=side.upper()
    if side not in ("BUY","SELL") or quantity <= 0 or price <= 0:
        raise ValueError("Ordre invalide")
    db=connect()
    try:
        with db.transaction():
            with db.cursor() as cur:
                cur.execute("SELECT cash FROM atlas_account WHERE id=1 FOR UPDATE")
                cash=float(cur.fetchone()["cash"])
                cur.execute("SELECT quantity,avg_cost FROM atlas_positions WHERE symbol=%s FOR UPDATE",(symbol,))
                pos=cur.fetchone()
                held=float(pos["quantity"]) if pos else 0.0
                avg=float(pos["avg_cost"]) if pos else 0.0
                gross=quantity*price
                fee=gross*fee_rate
                if side=="BUY":
                    if gross+fee>cash: raise ValueError("Liquidités insuffisantes")
                    if (held+quantity)*price > STARTING_CASH*MAX_POSITION_FRACTION: raise ValueError("Limite de taille de position atteinte")
                    new_qty=held+quantity
                    new_avg=((held*avg)+(quantity*price))/new_qty
                    cash-=gross+fee
                else:
                    if quantity>held+1e-9: raise ValueError("Vente supérieure à la position détenue")
                    new_qty=held-quantity
                    new_avg=avg if new_qty>1e-9 else 0.0
                    cash+=gross-fee
                cur.execute("UPDATE atlas_account SET cash=%s WHERE id=1",(cash,))
                cur.execute(
                    "INSERT INTO atlas_positions(symbol,quantity,avg_cost) VALUES(%s,%s,%s) ON CONFLICT(symbol) DO UPDATE SET quantity=EXCLUDED.quantity,avg_cost=EXCLUDED.avg_cost",
                    (symbol,new_qty,new_avg),
                )
                cur.execute(
                    "INSERT INTO atlas_prices(symbol,price,as_of) VALUES(%s,%s,%s) ON CONFLICT(symbol) DO UPDATE SET price=EXCLUDED.price,as_of=EXCLUDED.as_of",
                    (symbol,price,now()),
                )
                cur.execute(
                    "INSERT INTO atlas_trades(at,symbol,side,quantity,price,fee,reason,strategy) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (now(),symbol,side,quantity,price,fee,reason,"ATLAS-consensus-v2"),
                )
    finally:
        db.close()


def cycle_record(status, summary):
    db=connect()
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO atlas_cycles(at,status,summary) VALUES(%s,%s,%s)",(now(),status,summary))
        db.commit()
    finally:
        db.close()


def format_snapshot():
    s=snapshot()
    lines=["🤖 ATLAS — portefeuille fictif","Capital initial : {:.2f} USD".format(s["starting_cash"]),"Valeur estimée : {:.2f} USD".format(s["equity"]),"Résultat total : {:+.2f} USD ({:+.2f} %)".format(s["pnl"],s["return_pct"]),"Liquidités : {:.2f} USD".format(s["cash"]),"Positions :"]
    if not s["positions"]: lines.append("Aucune position pour le moment.")
    for p in s["positions"]:
        lines.append("{} : {:.6g} unité(s), valeur {:.2f} USD, résultat latent {:+.2f} USD".format(p["symbol"],p["quantity"],p["market_value"],p["pnl"]))
    lines.append("\nMode simulation uniquement : aucun ordre réel n'est envoyé.")
    return "\n".join(lines)
