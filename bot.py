import logging
import time
import os
from datetime import datetime

import pandas as pd
import pandas_ta as ta
import requests
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# ========================= CONFIG =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8669805705:AAEeEawbQ5U5d-G2hJGV-fJjHO1r_1IVVJE")
ALERT_INTERVAL = 30 * 60          # 30 minutes
DEFAULT_TF = "15m"

SUPPORTED_TF = ["1m", "5m", "15m", "1h", "4h", "1d"]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)


# ========================= DATABASE =========================
def init_db():
    conn = sqlite3.connect('trades.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    coin TEXT,
                    direction TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    entry_time TEXT,
                    exit_time TEXT,
                    pnl_percent REAL,
                    grade INTEGER,
                    feedback TEXT,
                    status TEXT DEFAULT 'OPEN'
                 )''')
    conn.commit()
    conn.close()

def save_trade(coin, direction, entry_price, confidence=0):
    conn = sqlite3.connect('trades.db')
    c = conn.cursor()
    c.execute("""INSERT INTO trades 
                 (coin, direction, entry_price, entry_time, confidence, status)
                 VALUES (?, ?, ?, ?, ?, 'OPEN')""",
              (coin.upper(), direction.upper(), entry_price, datetime.utcnow().isoformat(), confidence))
    conn.commit()
    trade_id = c.lastrowid
    conn.close()
    return trade_id

def close_trade(trade_id, exit_price, feedback=""):
    conn = sqlite3.connect('trades.db')
    c = conn.cursor()
    c.execute("SELECT entry_price, direction FROM trades WHERE id=?", (trade_id,))
    row = c.fetchone()
    if not row:
        return None
    entry = row[0]
    direction = row[1]
    pnl_percent = ((exit_price - entry) / entry) * 100 if direction == "LONG" else ((entry - exit_price) / entry) * 100
    
    if pnl_percent >= 10: grade = 10
    elif pnl_percent >= 6: grade = 9
    elif pnl_percent >= 3: grade = 7
    elif pnl_percent >= 0: grade = 5
    elif pnl_percent >= -5: grade = 3
    else: grade = 1
    
    c.execute("""UPDATE trades SET 
                 exit_price=?, exit_time=?, pnl_percent=?, grade=?, feedback=?, status='CLOSED'
                 WHERE id=?""",
              (exit_price, datetime.utcnow().isoformat(), round(pnl_percent, 2), grade, feedback, trade_id))
    conn.commit()
    conn.close()
    return grade, round(pnl_percent, 2)

def get_all_trades():
    conn = sqlite3.connect('trades.db')
    df = pd.read_sql_query("SELECT * FROM trades ORDER BY id DESC", conn)
    conn.close()
    return df


# ── KUCOIN ────────────────────────────────────────────────────────────────────
def fetch_kucoin(symbol, interval="1d"):
    tf_map = {"1m": "1min", "5m": "5min", "15m": "15min","1h": "1hour", "4h": "4hour", "1d": "1day"}
    pair = symbol.upper() + "-USDT"
    params = {"symbol": pair, "type": tf_map.get(interval, "1day")}
    r = requests.get("https://api.kucoin.com/api/v1/market/candles", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "200000":
        raise ValueError(f"KuCoin: {data.get('msg')}")
    candles = data["data"]
    if not candles or len(candles) < 15:
        raise ValueError("Not enough candles")
    df = pd.DataFrame(candles, columns=["ts", "open", "close", "high", "low", "volume", "turnover"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="s")
    df = df.sort_values("ts").set_index("ts").dropna()
    return df


# ── MEXC ──────────────────────────────────────────────────────────────────────
def fetch_mexc(symbol, interval="1d"):
    tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
    pair = symbol.upper() + "USDT"
    params = {"symbol": pair, "interval": tf_map.get(interval, "1d"), "limit": 200}
    r = requests.get("https://api.mexc.com/api/v3/klines", params=params, timeout=15)
    r.raise_for_status()
    candles = r.json()
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume","close_ts", "quote_vol", "trades", "taker_buy_base","taker_buy_quote", "ignore"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df = df.sort_values("ts").set_index("ts").dropna()
    return df


# ── GATE.IO ───────────────────────────────────────────────────────────────────
def fetch_gate(symbol, interval="1d"):
    tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
    pair = symbol.upper() + "_USDT"
    params = {"currency_pair": pair, "interval": tf_map.get(interval, "1d"), "limit": 200}
    r = requests.get("https://api.gateio.ws/api/v4/spot/candlesticks", params=params, timeout=15)
    r.raise_for_status()
    candles = r.json()
    df = pd.DataFrame(candles, columns=["ts", "volume", "close", "high", "low", "open", "base_volume", "is_closed"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="s")
    df = df.sort_values("ts").set_index("ts").dropna()
    return df


# ── OKX ──────────────────────────────────────────────────────────────────────
def fetch_okx(symbol, interval="1d"):
    tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}
    pair = symbol.upper() + "-USDT"
    params = {"instId": pair, "bar": tf_map.get(interval, "1D"), "limit": 200}
    r = requests.get("https://www.okx.com/api/v5/market/candles", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise ValueError("OKX Error")
    candles = data["data"]
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms")
    df["volume"] = pd.to_numeric(df["vol"], errors="coerce")
    df = df.sort_values("ts").set_index("ts").dropna(subset=["open","high","low","close"])
    return df


def fetch_ohlcv(symbol, interval="1d"):
    sources = [("KuCoin", fetch_kucoin), ("MEXC", fetch_mexc), ("Gate.io", fetch_gate), ("OKX", fetch_okx)]
    for name, fn in sources:
        try:
            return fn(symbol, interval)
        except Exception as e:
            log.warning(f"{name} failed")
            time.sleep(1)
    raise ValueError("All sources failed")


# ── ANALYSIS ──────────────────────────────────────────────────────────────────
def analyse(df):
    close = df["close"]
    high = df["high"]
    low = df["low"]
    df = df.copy()

    df["ema20"] = ta.ema(close, length=10)
    df["ema50"] = ta.ema(close, length=20)
    df["ema200"] = ta.ema(close, length=35)
    df["rsi"] = ta.rsi(close, length=7)
    df["atr"] = ta.atr(high, low, close, length=7)

    macd = ta.macd(close, fast=6, slow=13, signal=5)
    if macd is not None:
        df = pd.concat([df, macd], axis=1)

    bb = ta.bbands(close, length=10)
    if bb is not None:
        df = pd.concat([df, bb], axis=1)

    df.dropna(inplace=True)
    if len(df) < 5:
        raise ValueError("Not enough data")

    latest = df.iloc[-1]
    price = float(latest["close"])
    atr = float(latest["atr"])
    rsi = float(latest["rsi"])

    score = 0
    if float(latest["ema20"]) > float(latest["ema50"]): score += 2
    if rsi > 50: score += 2
    if macd is not None and float(latest.get("MACD_6_13_5", 0)) > float(latest.get("MACDs_6_13_5", 0)): score += 2

    confidence = round((score / 7) * 100)
    direction = "LONG 📈" if score >= 4 else "SHORT 📉" if score <= -3 else "NEUTRAL ⚖️"

    return {
        "price": round(price, 4),
        "direction": direction,
        "confidence": confidence,
        "rsi": round(rsi, 2),
        "atr": round(atr, 4),
        "entry": round(price, 4),
        "sl": round(price - 1.5 * atr, 4) if "LONG" in direction else round(price + 1.5 * atr, 4),
        "tp1": round(price + 1.5 * atr, 4) if "LONG" in direction else round(price - 1.5 * atr, 4),
        "score": score
    }


def format_signal(symbol, tf, sig):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    text = f"━━━━━━━━━━━━━━━━━━\n<b>{symbol}/USDT</b> | {tf}\n🕒 {now}\n━━━━━━━━━━━━━━━━━━\n\n"
    text += f"📊 <b>Signal:</b> {sig['direction']}\n🎯 <b>Confidence:</b> {sig['confidence']}%\n\n"
    text += f"Entry: {sig['entry']}\nSL: {sig['sl']}\nTP1: {sig['tp1']}\n"
    return text


# ========================= MENU =========================
def main_menu():
    keyboard = [
        [InlineKeyboardButton("🔎 Scanning", callback_data="scanning"),
         InlineKeyboardButton("🔔 Alerts", callback_data="alerts")],
        [InlineKeyboardButton("📊 Performance", callback_data="performance"),
         InlineKeyboardButton("ℹ️ Info", callback_data="info")],
        [InlineKeyboardButton("💡 Tips", callback_data="tips"),
         InlineKeyboardButton("❓ Help", callback_data="help")],
        [InlineKeyboardButton("👤 Contact Owner", callback_data="contact")]
    ]
    return InlineKeyboardMarkup(keyboard)


async def show_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = "🤖 **Trading Bot**\n\nWhat would you like to do? Tap a category below 👇"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "scanning":
        await query.edit_message_text("Use /signal BTC 15m", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)
    elif query.data == "alerts":
        await query.edit_message_text("Use /watch BTC 15m", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)
    elif query.data == "performance":
        await query.edit_message_text("Use /trades", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)
    else:
        await query.edit_message_text("Feature coming soon...", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)


# ========================= COMMANDS =========================
async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /signal BTC 15m")
        return
    symbol = ctx.args[0].upper()
    tf = ctx.args[1] if len(ctx.args) > 1 else DEFAULT_TF
    try:
        df = fetch_ohlcv(symbol, tf)
        sig = analyse(df)
        await update.message.reply_text(format_signal(symbol, tf, sig), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")


async def cmd_newtrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text("Usage: /newtrade BTC LONG 68500")
        return
    try:
        trade_id = save_trade(ctx.args[0], ctx.args[1], float(ctx.args[2]))
        await update.message.reply_text(f"✅ Trade #{trade_id} opened")
    except:
        await update.message.reply_text("Invalid format")


# ========================= MAIN =========================
async def post_init(application: Application):
    await application.bot.delete_my_commands()
    await application.bot.set_my_commands([
        ("start", "Open Menu"),
        ("menu", "Show Menu"),
        ("signal", "Get Signal"),
        ("newtrade", "New Trade"),
        ("trades", "View Trades"),
        ("watch", "Start Alerts"),
    ])

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", show_menu))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("newtrade", cmd_newtrade))
    app.add_handler(CallbackQueryHandler(button_handler))

    log.info("Bot Started")
    app.run_polling()

if __name__ == "__main__":
    main()
