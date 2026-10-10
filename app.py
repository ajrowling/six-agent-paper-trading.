import os
import time
import json
import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

APP_TITLE = "Six-Agent Paper Trading Team v2"
DB_PATH = os.getenv("PAPER_DB_PATH", "paper_trading.db")
SCAN_INTERVAL_DEFAULT = 60
MAX_RISK_PCT = 1.0
BINANCE_BASE = "https://api.binance.com"
BINANCE_KLINES = BINANCE_BASE + "/api/v3/klines"
BINANCE_TICKER = BINANCE_BASE + "/api/v3/ticker/24hr"

st.set_page_config(page_title=APP_TITLE, page_icon="🧪", layout="wide")

def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def connect():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL, closed_at TEXT,
        symbol TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL,
        strategy TEXT, entry REAL NOT NULL, exit REAL, stop REAL NOT NULL,
        target REAL NOT NULL, quantity REAL NOT NULL, notional REAL NOT NULL,
        risk_usd REAL NOT NULL, pnl REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL, scanner TEXT, fomo TEXT, sizer TEXT,
        reason TEXT, close_reason TEXT, fees_estimate REAL DEFAULT 0,
        meta_json TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
        symbol TEXT, event_type TEXT NOT NULL, decision TEXT, reason TEXT, meta_json TEXT
    )""")
    c.commit()
    return c

db_lock = threading.RLock()
conn = connect()

def setting(key, default):
    with db_lock:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try: return json.loads(row["value"])
    except Exception: return default

def save_setting(key, value):
    with db_lock:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))
        conn.commit()

def log_event(symbol, event_type, decision, reason, meta=None):
    with db_lock:
        conn.execute("INSERT INTO events(created_at,symbol,event_type,decision,reason,meta_json) VALUES(?,?,?,?,?,?)",
                     (utcnow(), symbol, event_type, decision, reason, json.dumps(meta or {})))
        conn.commit()

def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=10, headers={"User-Agent":"SixAgentPaperTrading/2.0"})
    r.raise_for_status()
    return r.json()

def binance_price(symbol):
    return float(get_json(BINANCE_TICKER, {"symbol": symbol})["lastPrice"])

def candles(symbol, interval="5m", limit=60):
    raw = get_json(BINANCE_KLINES, {"symbol": symbol, "interval": interval, "limit": limit})
    df = pd.DataFrame(raw, columns=["t","open","high","low","close","volume","ct","qv","trades","tb","tq","ignore"])
    for col in ["open","high","low","close","volume","qv"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def snapshot(symbol):
    t = get_json(BINANCE_TICKER, {"symbol": symbol})
    c5 = candles(symbol, "5m", 30)
    c15 = candles(symbol, "15m", 8)
    price = float(t["lastPrice"])
    move5 = (price / float(c5["close"].iloc[-2]) - 1) * 100
    move15 = (price / float(c15["close"].iloc[-2]) - 1) * 100
    # Use closed candles for recent support/resistance proxies.
    recent = c5.iloc[-13:-1]
    return {
        "symbol": symbol, "price": price,
        "change24": float(t["priceChangePercent"]),
        "quote_volume": float(t["quoteVolume"]),
        "move5": move5, "move15": move15,
        "support": float(recent["low"].min()),
        "resistance": float(recent["high"].max()),
        "ema9": float(c5["close"].ewm(span=9).mean().iloc[-2]),
        "ema21": float(c5["close"].ewm(span=21).mean().iloc[-2]),
        "atr": float((c5["high"]-c5["low"]).rolling(14).mean().iloc[-2]),
        "candles": c5.tail(3)[["open","high","low","close","volume"]].to_dict("records")
    }

def scanner_agent(s):
    # Transparent baseline heuristic, not a predictive AI model.
    if s["quote_volume"] < 250000:
        return {"decision":"SKIP", "side":None, "reason":"24h quote volume below $250,000."}
    if not (math.isfinite(s["atr"]) and s["atr"] > 0):
        return {"decision":"SKIP", "side":None, "reason":"Volatility data unavailable."}
    if s["price"] > s["ema9"] > s["ema21"] and s["move5"] > -1.5 and s["price"] >= s["support"]:
        return {"decision":"PASS", "side":"LONG", "reason":"Price above rising short-term EMA structure; heuristic trend filter passed."}
    if s["price"] < s["ema9"] < s["ema21"] and s["move5"] < 1.5 and s["price"] <= s["resistance"]:
        return {"decision":"PASS", "side":"SHORT", "reason":"Price below short-term EMA structure; paper short heuristic passed."}
    return {"decision":"SKIP", "side":None, "reason":"No clean EMA trend alignment; skip rather than force a setup."}

def anti_fomo_agent(s, side):
    reasons = []
    if s["move5"] >= 4.0: reasons.append("5-minute move is +4% or more.")
    if s["move15"] >= 8.0: reasons.append("15-minute move is +8% or more.")
    if s["change24"] >= 30.0: reasons.append("24-hour move is +30% or more.")
    if s["price"] > s["resistance"] * 1.01: reasons.append("Price is more than 1% above recent resistance.")
    if s["quote_volume"] < 250000: reasons.append("Volume/liquidity proxy is below threshold.")
    if reasons:
        return {"decision":"BLOCK", "reason":" ".join(reasons)}
    return {"decision":"APPROVE", "reason":"No configured anti-FOMO threshold triggered; this does not guarantee safety."}

def sizer_agent(balance, entry, stop, target, risk_pct, side):
    if balance <= 0 or entry <= 0 or stop <= 0 or target <= 0:
        return {"decision":"REJECT", "reason":"Prices and balance must be positive."}
    if side == "LONG" and not stop < entry < target:
        return {"decision":"REJECT", "reason":"For LONG, stop < entry < target is required."}
    if side == "SHORT" and not target < entry < stop:
        return {"decision":"REJECT", "reason":"For SHORT, target < entry < stop is required."}
    risk_pct = min(float(risk_pct), MAX_RISK_PCT)
    risk_usd = balance * risk_pct / 100
    per_unit_risk = abs(entry-stop)
    qty = risk_usd / per_unit_risk
    notional = qty * entry
    rr = abs(target-entry) / per_unit_risk
    # Guard against absurd notional sizes from very tight stops. No leverage is used.
    if notional > balance * 3:
        return {"decision":"REJECT", "reason":"Calculated notional exceeds 3x paper balance; this spot-style simulator does not use leverage."}
    if rr < 1.2:
        return {"decision":"REJECT", "reason":"Reward/risk is below the 1.2 minimum for this prototype."}
    return {"decision":"APPROVE", "risk_usd":risk_usd, "qty":qty, "notional":notional, "rr":rr,
            "reason":"Position quantity sized from max account risk divided by entry-to-stop distance."}

def open_trades():
    with db_lock:
        return conn.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id").fetchall()

def balance_and_stats():
    start = float(setting("starting_balance", 1000.0))
    with db_lock:
        rows = conn.execute("SELECT pnl,status FROM trades").fetchall()
    realized = sum(float(r["pnl"] or 0) for r in rows if r["status"] == "CLOSED")
    closed = [float(r["pnl"] or 0) for r in rows if r["status"] == "CLOSED"]
    return start + realized, realized, closed

def close_trade(tr, exit_price, reason):
    gross = (exit_price-tr["entry"])*tr["quantity"] if tr["side"]=="LONG" else (tr["entry"]-exit_price)*tr["quantity"]
    # Simple estimated round-trip fee: 0.1% of entry + exit notional. Actual fees vary.
    fees = (tr["entry"]*tr["quantity"] + exit_price*tr["quantity"]) * 0.001
    pnl = gross - fees
    with db_lock:
        conn.execute("UPDATE trades SET exit=?, pnl=?, status='CLOSED', closed_at=?, close_reason=?, fees_estimate=? WHERE id=? AND status='OPEN'",
                     (exit_price, pnl, utcnow(), reason, fees, tr["id"]))
        conn.commit()
    log_event(tr["symbol"], "POSITION_CLOSED", "CLOSED", reason, {"trade_id":tr["id"], "exit":exit_price, "pnl":pnl, "estimated_fees":fees})

def auto_scan(symbols, risk_pct, enabled):
    if not enabled:
        return
    if open_trades():
        return
    balance, _, _ = balance_and_stats()
    for symbol in symbols:
        if open_trades():
            break
        try:
            s = snapshot(symbol)
            scan = scanner_agent(s)
            fomo = anti_fomo_agent(s, scan["side"]) if scan["side"] else {"decision":"BLOCK","reason":"Scanner did not pass."}
            if scan["decision"] != "PASS" or fomo["decision"] != "APPROVE":
                log_event(symbol, "SCAN", "SKIP", f"Scanner: {scan['reason']} Anti-FOMO: {fomo['reason']}", s)
                continue
            side = scan["side"]
            # Stops based on recent support/resistance with an ATR floor.
            if side == "LONG":
                stop = min(s["support"], s["price"] - max(s["atr"]*1.5, s["price"]*0.005))
                stop = max(stop, s["price"]*0.90)
                target = s["price"] + 2.0*(s["price"]-stop)
            else:
                stop = max(s["resistance"], s["price"] + max(s["atr"]*1.5, s["price"]*0.005))
                stop = min(stop, s["price"]*1.10)
                target = s["price"] - 2.0*(stop-s["price"])
            sized = sizer_agent(balance, s["price"], stop, target, risk_pct, side)
            if sized["decision"] != "APPROVE":
                log_event(symbol, "SIZER", "REJECT", sized["reason"], s)
                continue
            with db_lock:
                # Recheck single-open-position constraint at insertion time.
                count = conn.execute("SELECT COUNT(*) AS n FROM trades WHERE status='OPEN'").fetchone()["n"]
                if count:
                    break
                conn.execute("""INSERT INTO trades(created_at,symbol,market,side,strategy,entry,stop,target,quantity,notional,risk_usd,status,scanner,fomo,sizer,reason,meta_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (utcnow(),symbol,"CRYPTO","PAPER_"+side,s["price"],s["price"],stop,target,sized["qty"],sized["notional"],sized["risk_usd"],"OPEN","PASS","APPROVE","APPROVE",scan["reason"]+" "+sized["reason"],json.dumps(s)))
                conn.commit()
            log_event(symbol, "POSITION_OPENED", "OPEN", scan["reason"], {"side":side,"snapshot":s})
        except Exception as e:
            log_event(symbol, "SCAN_ERROR", "ERROR", str(e), {})

def auto_manage_exits():
    for tr in open_trades():
        try:
            price = binance_price(tr["symbol"])
            if tr["side"] == "LONG":
                if price <= tr["stop"]:
                    close_trade(tr, tr["stop"], "Simulated stop-loss touched (fill assumed at stop; gaps/slippage not modeled).")
                elif price >= tr["target"]:
                    close_trade(tr, tr["target"], "Simulated target touched (fill assumed at target; gaps/slippage not modeled).")
            else:
                if price >= tr["stop"]:
                    close_trade(tr, tr["stop"], "Simulated stop-loss touched (fill assumed at stop; gaps/slippage not modeled).")
                elif price <= tr["target"]:
                    close_trade(tr, tr["target"], "Simulated target touched (fill assumed at target; gaps/slippage not modeled).")
        except Exception as e:
            log_event(tr["symbol"], "EXIT_CHECK_ERROR", "ERROR", str(e), {"trade_id":tr["id"]})

def auto_loop():
    while True:
        try:
            enabled = bool(setting("auto_enabled", False))
            symbols = setting("symbols", ["BTCUSDT","ETHUSDT","DOGEUSDT","SOLUSDT"])
            risk = min(float(setting("risk_pct", 0.5)), MAX_RISK_PCT)
            if enabled:
                auto_manage_exits()
                if not open_trades():
                    auto_scan(symbols, risk, True)
        except Exception:
            pass
        time.sleep(max(15, int(setting("scan_interval", SCAN_INTERVAL_DEFAULT))))

# Streamlit reruns can create threads; use a process-level guard.
if not hasattr(st, "_paper_worker_started"):
    st._paper_worker_started = True
    threading.Thread(target=auto_loop, daemon=True, name="paper-trading-loop").start()

st.title("🧪 Six-Agent Trading Team v2")
st.caption("Mobile-friendly dashboard • Fully automatic PAPER MODE • No API keys, wallet, or live-order capability")
st.error("PAPER ONLY. The automatic loop runs only while this app process is alive. Free hosting may sleep, restart, or lose local files. This is a prototype, not a profitability claim.")

balance, realized, pnl_list = balance_and_stats()
positions = open_trades()
wins = sum(p > 0 for p in pnl_list)
losses = sum(p < 0 for p in pnl_list)
wr = wins/len(pnl_list)*100 if pnl_list else 0
cols = st.columns(5)
cols[0].metric("Paper balance", f"${balance:,.2f}")
cols[1].metric("Realized P&L", f"${realized:,.2f}")
cols[2].metric("Closed trades", len(pnl_list))
cols[3].metric("Win rate", f"{wr:.1f}%")
cols[4].metric("Open positions", len(positions))

with st.sidebar:
    st.header("Paper controls")
    st.write("**Hard limit: 1% maximum risk**")
    start_balance = st.number_input("Starting paper balance ($)", 100.0, 10000000.0, float(setting("starting_balance",1000.0)), step=100.0)
    risk = st.number_input("Risk per trade (%)", 0.1, 1.0, min(float(setting("risk_pct",0.5)),1.0), step=0.1)
    symbols_text = st.text_input("Crypto pairs, comma separated", ",".join(setting("symbols",["BTCUSDT","ETHUSDT","DOGEUSDT","SOLUSDT"])))
    interval = st.number_input("Scan interval seconds", min_value=15, max_value=3600, value=int(setting("scan_interval",60)), step=15)
    enabled_now = bool(setting("auto_enabled",False))
    enable = st.toggle("Enable automatic paper trading", value=enabled_now)
    if st.button("Save settings", use_container_width=True):
        symbols = list(dict.fromkeys(x.strip().upper() for x in symbols_text.split(",") if x.strip()))
        # Avoid empty lists and non-USDT pairs. Exchange endpoint will validate each symbol.
        symbols = [x for x in symbols if x.endswith("USDT")][:30]
        if not symbols:
            st.error("Enter at least one Binance USDT pair.")
        else:
            save_setting("starting_balance", start_balance)
            save_setting("risk_pct", min(risk,1.0))
            save_setting("symbols", symbols)
            save_setting("scan_interval", int(interval))
            save_setting("auto_enabled", bool(enable))
            log_event("", "SETTINGS", "SAVED", f"Auto trading {'enabled' if enable else 'disabled'}; max risk {min(risk,1.0)}%.")
            st.success("Saved. The background loop checks these settings while the app process is running.")
            st.rerun()
    if enable:
        st.warning("Automatic paper trading is ENABLED.")
    else:
        st.info("Automatic paper trading is paused.")
    if st.button("Emergency pause automation", type="primary", use_container_width=True):
        save_setting("auto_enabled", False)
        log_event("", "AUTOMATION", "PAUSED", "Emergency pause clicked.")
        st.success("Automation paused.")
        st.rerun()

tab1, tab2, tab3, tab4 = st.tabs(["📍 Open position", "📊 Market scanner", "📒 Trade & event log", "🧠 Agent rules"])

with tab1:
    st.subheader("Automatic simulated exit management")
    positions = open_trades()
    if not positions:
        st.info("No open paper position. When automation is enabled, the system scans configured crypto pairs and can open at most one simulated position.")
    for tr in positions:
        st.markdown(f"**Trade #{tr['id']} — {tr['symbol']} — {tr['side']}**")
        a,b,c,d = st.columns(4)
        a.metric("Entry", f"${tr['entry']:.8g}")
        b.metric("Stop loss", f"${tr['stop']:.8g}")
        c.metric("Target", f"${tr['target']:.8g}")
        d.metric("Planned risk", f"${tr['risk_usd']:.2f}")
        try:
            now_price = binance_price(tr["symbol"])
            unreal = (now_price-tr["entry"])*tr["quantity"] if tr["side"]=="LONG" else (tr["entry"]-now_price)*tr["quantity"]
            st.write(f"Latest public price: **${now_price:.8g}** · Approx. unrealized gross P&L: **${unreal:.2f}**")
        except Exception as e:
            st.warning(f"Could not refresh price: {e}")
        if st.button(f"Manual paper close trade #{tr['id']}", key=f"manualclose{tr['id']}"):
            try:
                px = binance_price(tr["symbol"])
                close_trade(tr, px, "Manual paper close requested by user.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not close: {e}")

with tab2:
    st.subheader("Public-data market snapshot")
    symbols = setting("symbols", ["BTCUSDT","ETHUSDT","DOGEUSDT","SOLUSDT"])
    if st.button("Refresh market table"):
        rows = []
        for sym in symbols:
            try:
                s = snapshot(sym)
                sc = scanner_agent(s)
                fo = anti_fomo_agent(s, sc["side"]) if sc["side"] else {"decision":"BLOCK","reason":"No scanner setup"}
                rows.append({"Symbol":sym,"Price":s["price"],"24h %":s["change24"],"5m %":s["move5"],"15m %":s["move15"],"24h quote vol $":s["quote_volume"],"Scanner":sc["decision"],"Side":sc["side"] or "—","Anti-FOMO":fo["decision"],"Reason":sc["reason"]})
            except Exception as e:
                rows.append({"Symbol":sym,"Scanner":"ERROR","Reason":str(e)})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption("This first version scans Binance-listed crypto spot pairs only. Gold is shown as a separate module below because Binance spot data does not provide XAUUSD.")

with tab3:
    st.subheader("Audit history")
    with db_lock:
        trades = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 1000").fetchall()
        events = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1000").fetchall()
    if trades:
        df = pd.DataFrame([dict(x) for x in trades])
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button("Download trades CSV", df.to_csv(index=False).encode(), "paper_trades.csv", "text/csv")
    else:
        st.info("No trade records yet.")
    if events:
        edf = pd.DataFrame([dict(x) for x in events])
        st.markdown("#### Agent decisions, skipped setups and system events")
        st.dataframe(edf, use_container_width=True, hide_index=True)
        st.download_button("Download events CSV", edf.to_csv(index=False).encode(), "paper_events.csv", "text/csv")
    if pnl_list:
        st.markdown("#### Closed-trade statistics")
        a,b,c,d = st.columns(4)
        a.metric("Wins", wins); b.metric("Losses", losses)
        a2 = [x for x in pnl_list if x>0]; l2=[x for x in pnl_list if x<0]
        c.metric("Average win", f"${sum(a2)/len(a2):.2f}" if a2 else "—")
        d.metric("Average loss", f"${sum(l2)/len(l2):.2f}" if l2 else "—")
        st.write(f"Best: ${max(pnl_list):.2f} · Worst: ${min(pnl_list):.2f}")

with tab4:
    st.subheader("Six agents and their current implementation")
    st.markdown("""
1. **Scanner:** simple EMA trend alignment, volume threshold and recent support/resistance proxies.
2. **Sizer:** caps account risk at 1%, requires stop and target, checks reward/risk and blocks oversized notionals.
3. **Paper Buyer:** inserts a local simulated position only; there is no live-order code.
4. **Prediction Research:** reserved for a future separate research-only module; not enabled in the automated loop.
5. **Anti-FOMO:** blocks configured sharp moves, large daily pumps, breakout extension and low volume.
6. **Logger:** records opens, closes, skipped scans, errors, settings and exports CSV.

### Gold (XAUUSD) status
Gold is not yet connected in this build because it requires a separate market-data provider. The dashboard must not fabricate a gold price or pretend that crypto prices are XAUUSD. The next integration can use a trusted quote provider or an MT5 demo terminal adapter. Until then, automatic execution is **crypto only**, and gold is explicitly not scanned.

### Simulated exits
The background loop polls open crypto positions and closes them when the latest observed price touches the stop or target. This is not tick-by-tick monitoring. If the app sleeps, loses connectivity, or restarts, checks pause. Stop fills are assumed at the stop price; real gaps and slippage may be worse.

### Important
No LLM API keys are used in this version. The agents are independent functions with explicit deterministic rules, not six autonomous AI chats. Treat all performance as experimental; fees are approximated and slippage is not fully modeled.
""")
    st.subheader("Prediction research")
    st.write("Prediction-market research is intentionally kept separate and is not included in automatic trading.")

st.caption("v2 prototype • paper-only • no real orders possible")
