"""ATLAS paper-trading ledger. Simulated orders only; never connects to a broker."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.getenv("ATLAS_DB_PATH", "/tmp/atlas.sqlite3")
STARTING_CASH = float(os.getenv("ATLAS_STARTING_CASH", "10000"))
WATCHLIST = {
    "ORCL": "Oracle", "NVDA": "NVIDIA", "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq-100 ETF", "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum",
}
MAX_POSITION_FRACTION = 0.15
MAX_TRADE_FRACTION = 0.05


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), cash REAL NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, quantity REAL NOT NULL, avg_cost REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS prices (symbol TEXT PRIMARY KEY, price REAL NOT NULL, as_of TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL, reason TEXT NOT NULL, strategy TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL);
    """)
    db.execute("INSERT OR IGNORE INTO account(id,cash,created_at) VALUES(1,?,?)", (STARTING_CASH, now()))
    db.commit()
    return db


def snapshot():
    db = connect()
    account = db.execute("SELECT cash,created_at FROM account WHERE id=1").fetchone()
    positions = []
    value = float(account["cash"])
    for p in db.execute("SELECT symbol,quantity,avg_cost FROM positions WHERE quantity > 0.00000001"):
        price = db.execute("SELECT price,as_of FROM prices WHERE symbol=?", (p["symbol"],)).fetchone()
        current = float(price["price"]) if price else float(p["avg_cost"])
        market_value = p["quantity"] * current
        value += market_value
        positions.append({"symbol":p["symbol"],"name":WATCHLIST.get(p["symbol"],p["symbol"]),"quantity":p["quantity"],"avg_cost":p["avg_cost"],"price":current,"market_value":market_value,"pnl":market_value-p["quantity"]*p["avg_cost"],"as_of":price["as_of"] if price else "prix indisponible"})
    trades = db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 10").fetchall()
    result = {"cash":float(account["cash"]),"equity":value,"pnl":value-STARTING_CASH,"return_pct":(value/STARTING_CASH-1)*100 if STARTING_CASH else 0,"positions":positions,"trades":[dict(t) for t in trades],"starting_cash":STARTING_CASH}
    db.close()
    return result


def record_price(symbol, price, as_of=None):
    if price <= 0:
        raise ValueError("Prix invalide")
    db=connect()
    db.execute("INSERT INTO prices(symbol,price,as_of) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET price=excluded.price,as_of=excluded.as_of",(symbol,float(price),as_of or now()))
    db.commit(); db.close()


def paper_order(symbol, side, quantity, price, reason, fee_rate=0.001):
    """Execute a simulated order with cash and position checks."""
    side=side.upper()
    if side not in ("BUY","SELL") or quantity <= 0 or price <= 0:
        raise ValueError("Ordre invalide")
    db=connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        cash=float(db.execute("SELECT cash FROM account WHERE id=1").fetchone()["cash"])
        pos=db.execute("SELECT quantity,avg_cost FROM positions WHERE symbol=?",(symbol,)).fetchone()
        held=float(pos["quantity"]) if pos else 0.0
        avg=float(pos["avg_cost"]) if pos else 0.0
        gross=quantity*price; fee=gross*fee_rate
        if side=="BUY":
            if gross+fee>cash: raise ValueError("Liquidités insuffisantes")
            if (held+quantity)*price > STARTING_CASH*MAX_POSITION_FRACTION: raise ValueError("Limite de taille de position atteinte")
            new_qty=held+quantity
            new_avg=((held*avg)+(quantity*price))/new_qty
            cash-=gross+fee
        else:
            if quantity>held+1e-9: raise ValueError("Vente supérieure à la position détenue")
            new_qty=held-quantity; new_avg=avg if new_qty>1e-9 else 0.0
            cash+=gross-fee
        db.execute("UPDATE account SET cash=? WHERE id=1",(cash,))
        db.execute("INSERT INTO positions(symbol,quantity,avg_cost) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity,avg_cost=excluded.avg_cost",(symbol,new_qty,new_avg))
        db.execute("INSERT INTO prices(symbol,price,as_of) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET price=excluded.price,as_of=excluded.as_of",(symbol,price,now()))
        db.execute("INSERT INTO trades(at,symbol,side,quantity,price,fee,reason,strategy) VALUES(?,?,?,?,?,?,?,?)",(now(),symbol,side,quantity,price,fee,reason,"ATLAS-baseline-v1"))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally: db.close()


def cycle_record(status, summary):
    db=connect()
    db.execute("INSERT INTO cycles(at,status,summary) VALUES(?,?,?)",(now(),status,summary))
    db.commit(); db.close()


def format_snapshot():
    s=snapshot()
    lines=["🤖 ATLAS — portefeuille fictif","Capital initial : {:.2f} €".format(s["starting_cash"]),"Valeur estimée : {:.2f} €".format(s["equity"]),"Résultat total : {:+.2f} € ({:+.2f} %)".format(s["pnl"],s["return_pct"]),"Liquidités : {:.2f} €".format(s["cash"]),"Positions :"]
    if not s["positions"]: lines.append("Aucune position pour le moment.")
    for p in s["positions"]:
        lines.append("{} : {:.6g} unité(s), valeur {:.2f} €, résultat latent {:+.2f} €".format(p["symbol"],p["quantity"],p["market_value"],p["pnl"]))
    lines.append("\nMode simulation uniquement : aucun ordre réel n'est envoyé.")
    return "\n".join(lines)
