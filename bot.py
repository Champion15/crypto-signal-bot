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

# CONFIG
BOT_TOKEN = os.getenv("BOT_TOKEN", "8669805705:AAEeEawbQ5U5d-G2hJGV-fJjHO1r_1IVVJE")
ALERT_INTERVAL = 30 * 60          # 30 minutes
DEFAULT_TF = "15m"

SUPPORTED_TF = ["1m", "5m", "15m", "1h", "4h", "1d"]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)


# ========================= DATABASE FOR TRADE JOURNAL =========================
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
    if direction == "LONG":
        pnl_percent = ((exit_price - entry) / entry) * 100
    else:
        pnl_percent = ((entry - exit_price) / entry) * 100
    
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
    tf_map = {
        "1m": "1min", "5m": "5min", "15m": "15min",
        "1h": "1hour", "4h": "4hour", "1d": "1day",
    }
    pair = symbol.upper() + "-USDT"
    params = {"symbol": pair, "type": tf_map.get(interval, "1day")}
    r = requests.get("https://api.kucoin.com/api/v1/market/candles", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "200000":
        raise ValueError(f"KuCoin: {data.get('msg')}")
    candles = data["data"]
    if not candles or len(candles) < 15:
        raise ValueError(f"KuCoin: not enough candles ({len(candles) if candles else 0})")
    df = pd.DataFrame(candles, columns=["ts", "open", "close", "high", "low", "volume", "turnover"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="s")
    df = df.sort_values("ts").set_index("ts")
    df.dropna(inplace=True)
    log.info(f"KuCoin: {len(df)} candles for {pair} ({interval})")
    return df


# ── MEXC ──────────────────────────────────────────────────────────────────────
def fetch_mexc(symbol, interval="1d"):
    tf_map = {
        "1m": "1m", "5m": "5m", "15m": "15m",
        "1h": "1h", "4h": "4h", "1d": "1d",
    }
    pair = symbol.upper() + "USDT"
    params = {"symbol": pair, "interval": tf_map.get(interval, "1d"), "limit": 200}
    r = requests.get("https://api.mexc.com/api/v3/klines", params=params, timeout=15)
    r.raise_for_status()
    candles = r.json()
    if not candles or len(candles) < 15:
        raise ValueError(f"MEXC: not enough candles ({len(candles) if candles else 0})")
    df = pd.DataFrame(candles, columns=[
        "ts", "open", "high", "low", "close", "volume",
        "close_ts", "quote_vol", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df = df.sort_values("ts").set_index("ts")
    df.dropna(inplace=True)
    log.info(f"MEXC: {len(df)} candles for {pair} ({interval})")
    return df


# ── GATE.IO ───────────────────────────────────────────────────────────────────
def fetch_gate(symbol, interval="1d"):
    tf_map = {
        "1m": "1m", "5m": "5m", "15m": "15m",
        "1h": "1h", "4h": "4h", "1d": "1d",
    }
    pair = symbol.upper() + "_USDT"
    params = {"currency_pair": pair, "interval": tf_map.get(interval, "1d"), "limit": 200}
    r = requests.get("https://api.gateio.ws/api/v4/spot/candlesticks", params=params, timeout=15)
    r.raise_for_status()
    candles = r.json()
    if not candles or len(candles) < 15:
        raise ValueError(f"Gate.io: not enough candles ({len(candles) if candles else 0})")
    df = pd.DataFrame(candles, columns=["ts", "volume", "close", "high", "low", "open", "base_volume", "is_closed"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="s")
    df = df.sort_values("ts").set_index("ts")
    df.dropna(inplace=True)
    log.info(f"Gate.io: {len(df)} candles for {pair} ({interval})")
    return df


# ── OKX ──────────────────────────────────────────────────────────────────────
def fetch_okx(symbol, interval="1d"):
    tf_map = {
        "1m": "1m", "5m": "5m", "15m": "15m",
        "1h": "1H", "4h": "4H", "1d": "1D",
    }
    pair = symbol.upper() + "-USDT"
    params = {"instId": pair, "bar": tf_map.get(interval, "1D"), "limit": 200}
    r = requests.get("https://www.okx.com/api/v5/market/candles", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise ValueError(f"OKX: {data.get('msg')}")
    candles = data["data"]
    if not candles or len(candles) < 15:
        raise ValueError(f"OKX: not enough candles ({len(candles) if candles else 0})")
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms")
    df["volume"] = pd.to_numeric(df["vol"], errors="coerce")
    df = df.sort_values("ts").set_index("ts")
    df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    log.info(f"OKX: {len(df)} candles for {pair} ({interval})")
    return df


# ── MAIN FETCH ────────────────────────────────────────────────────────────────
def fetch_ohlcv(symbol, interval="1d"):
    sources = [
        ("KuCoin", fetch_kucoin),
        ("MEXC", fetch_mexc),
        ("Gate.io", fetch_gate),
        ("OKX", fetch_okx),
    ]
    last_error = None
    for name, fetch_fn in sources:
        try:
            return fetch_fn(symbol, interval)
        except Exception as e:
            log.warning(f"{name} failed: {e}")
            last_error = e
            time.sleep(1)
    raise ValueError(f"All sources failed. Last error: {last_error}")


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
    if macd is not None and not macd.empty:
        df = pd.concat([df, macd], axis=1)

    bb = ta.bbands(close, length=10)
    if bb is not None and not bb.empty:
        df = pd.concat([df, bb], axis=1)

    df.dropna(inplace=True)
    log.info(f"Rows after dropna: {len(df)}")

    if len(df) < 5:
        raise ValueError("Not enough valid candles after calculation")

    latest = df.iloc[-1]

    price = float(latest["close"])
    atr = float(latest["atr"])
    rsi = float(latest["rsi"])
    ema20 = float(latest["ema20"])
    ema50 = float(latest["ema50"])
    ema200 = float(latest["ema200"])
    macd_val = float(latest.get("MACD_6_13_5", 0))
    macd_sig = float(latest.get("MACDs_6_13_5", 0))

    bullish_ema = ema20 > ema50 > ema200
    bearish_ema = ema20 < ema50 < ema200
    macd_bull = macd_val > macd_sig
    price_above_ema = price > ema20 > ema50

    score = 0
    if bullish_ema: score += 2
    elif bearish_ema: score -= 2
    if macd_bull and macd_val > 0: score += 2
    elif macd_bull: score += 1
    if rsi > 50 and bullish_ema: score += 1
    if price_above_ema: score += 1

    confidence = round((abs(score) / 7) * 100)

    if score >= 4:
        direction = "LONG 📈"
    elif score <= -4:
        direction = "SHORT 📉"
    else:
        direction = "NEUTRAL ⚖️"

    entry = sl = tp1 = tp2 = tp3 = None
    rr = 0

    if "LONG" in direction:
        entry = round(price, 4)
        sl = round(price - 1.5 * atr, 4)
        tp1 = round(price + 1.5 * atr, 4)
        tp2 = round(price + 3.0 * atr, 4)
        tp3 = round(price + 5.0 * atr, 4)
    elif "SHORT" in direction:
        entry = round(price, 4)
        sl = round(price + 1.5 * atr, 4)
        tp1 = round(price - 1.5 * atr, 4)
        tp2 = round(price - 3.0 * atr, 4)
        tp3 = round(price - 5.0 * atr, 4)

    if entry and sl and sl != entry:
        rr = round(abs(tp2 - entry) / abs(sl - entry), 2)

    return {
        "price": round(price, 4),
        "direction": direction,
        "score": score,
        "confidence": confidence,
        "rsi": round(rsi, 2),
        "atr": round(atr, 4),
        "ema20": round(ema20, 4),
        "ema50": round(ema50, 4),
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr": rr,
        "macd_hist": round(macd_val - macd_sig, 4) if 'macd_hist' in locals() else 0,
    }


# ── FORMAT MESSAGE ────────────────────────────────────────────────────────────
def format_signal(symbol, tf, sig):
    stars = "⭐" * abs(sig["score"])
    confidence_bar = "🟩" * (sig["confidence"] // 20) + "⬜" * (5 - sig["confidence"] // 20)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>{symbol}/USDT</b> | {tf}\n"
        f"🕒 {now}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>Signal:</b> {sig['direction']} {stars}\n"
        f"🎯 <b>Confidence:</b> {sig['confidence']}% {confidence_bar}\n\n"
        "<b>Indicators</b>\n"
        f"RSI: {sig['rsi']}\n"
        f"EMA Short: {sig['ema20']}\n"
        f"EMA Mid: {sig['ema50']}\n"
        f"ATR: {sig['atr']}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    if sig.get("entry"):
        text += (
            "<b>Trade Setup</b>\n\n"
            f"🎯 Entry: {sig['entry']}\n"
            f"🛑 Stop Loss: {sig['sl']}\n\n"
            f"✅ TP1: {sig['tp1']}\n"
            f"✅ TP2: {sig['tp2']}\n"
            f"✅ TP3: {sig['tp3']}\n\n"
            f"📐 Risk Reward: 1:{sig['rr']}\n\n"
            "━━━━━━━━━━━━━━━━━━"
        )
    return text


# ── INTERACTIVE MENU ─────────────────────────────────────────────────────────
def main_menu():
    keyboard = [
        [InlineKeyboardButton("🔎 Scanning", callback_data="menu_scanning"),
         InlineKeyboardButton("🔔 Alerts", callback_data="menu_alerts")],
        [InlineKeyboardButton("📊 Performance", callback_data="menu_performance"),
         InlineKeyboardButton("ℹ️ Info", callback_data="menu_info")],
        [InlineKeyboardButton("💡 Tips", callback_data="menu_tips"),
         InlineKeyboardButton("❓ Help", callback_data="menu_help")],
        [InlineKeyboardButton("👤 Contact Owner", callback_data="menu_contact")]
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
    data = query.data

    if data == "menu_scanning":
        await query.edit_message_text("🔎 **Scanning**\n\nUse `/signal BTC 15m`", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)
    elif data == "menu_alerts":
        await query.edit_message_text("🔔 **Alerts**\n\nUse `/watch BTC 15m`", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)
    elif data == "menu_performance":
        await query.edit_message_text("📊 **Performance**\n\nUse `/trades`", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)
    elif data == "menu_info":
        await query.edit_message_text("ℹ️ **Bot Info**\n\nSignal + Trade Journal + Auto Grade", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)
    elif data == "menu_tips":
        await query.edit_message_text("💡 **Tips**\n\nOnly trade when confidence ≥ 80%", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)
    elif data == "menu_help":
        await cmd_help(update, ctx)
    elif data == "menu_contact":
        await query.edit_message_text("👤 Contact Owner: @YourUsername", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)


# ── TRADE JOURNAL COMMANDS ─────────────────────────────────────────────────────
async def cmd_newtrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text("Usage: `/newtrade BTC LONG 68500`", parse_mode=ParseMode.HTML)
        return
    try:
        coin = ctx.args[0].upper()
        direction = ctx.args[1].upper()
        entry_price = float(ctx.args[2])
        trade_id = save_trade(coin, direction, entry_price)
        await update.message.reply_text(f"✅ Trade #{trade_id} opened!\n{direction} {coin} @ {entry_price}", parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text("❌ Invalid format.")


async def cmd_closetrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: `/closetrade ID EXIT_PRICE`", parse_mode=ParseMode.HTML)
        return
    try:
        trade_id = int(ctx.args[0])
        exit_price = float(ctx.args[1])
        feedback = " ".join(ctx.args[2:]) if len(ctx.args) > 2 else ""
        result = close_trade(trade_id, exit_price, feedback)
        if result:
            grade, pnl = result
            await update.message.reply_text(f"✅ Trade #{trade_id} closed\nP&L: {pnl:+.2f}%\nGrade: {grade}/10 ⭐", parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text("❌ Invalid input.")


async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    df = get_all_trades()
    if df.empty:
        await update.message.reply_text("No trades recorded yet.")
        return
    text = "<b>📊 Trade Journal</b>\n\n"
    for _, row in df.head(10).iterrows():
        status = "🟢 CLOSED" if row['status'] == 'CLOSED' else "🟡 OPEN"
        text += f"#{row['id']} | {row['direction']} {row['coin']} | {status}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── ALERT (30 MINUTES + ≥80% CONFIDENCE) ─────────────────────────────────────
async def auto_alert(ctx: ContextTypes.DEFAULT_TYPE):
    job = ctx.job
    symbol = job.data["symbol"]
    tf = job.data.get("tf", DEFAULT_TF)
    try:
        df = fetch_ohlcv(symbol, tf)
        signal = analyse(df)
        if signal["confidence"] >= 80:
            text = f"🚨 <b>HIGH CONFIDENCE ALERT</b> — {signal['confidence']}%\n\n" + format_signal(symbol, tf, signal)
            await ctx.bot.send_message(chat_id=job.chat_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error(f"Alert error {symbol}: {e}")


async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage:\n/watch BTC 15m")
        return
    symbol = ctx.args[0].upper().replace("USDT", "")
    tf = ctx.args[1].lower() if len(ctx.args) > 1 else DEFAULT_TF
    chat_id = update.effective_chat.id
    job_name = f"alert_{chat_id}_{symbol}"
    ctx.job_queue.run_repeating(auto_alert, interval=ALERT_INTERVAL, first=10, chat_id=chat_id, name=job_name, data={"symbol": symbol, "tf": tf})
    await update.message.reply_text(f"✅ Watching {symbol} every 30 mins (≥80% only)")


# ── BOT HANDLERS ──────────────────────────────────────────────────────────────
async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage:\n/signal BTC\n/signal ETH 15m")
        return
    symbol = args[0].upper().replace("USDT", "")
    tf = args[1].lower() if len(args) > 1 else DEFAULT_TF
    if tf not in SUPPORTED_TF:
        await update.message.reply_text("Supported timeframes:\n1m, 5m, 15m, 1h, 4h, 1d")
        return
    await update.message.reply_text(f"⏳ Analysing {symbol}/USDT on {tf}...")
    try:
        df = fetch_ohlcv(symbol, tf)
        signal = analyse(df)
        text = format_signal(symbol, tf, signal)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error(e)
        await update.message.reply_text(f"❌ Error:\n{str(e)}")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>Crypto Signal Bot + Trade Journal</b>\n\n"
        "Use /menu to open the beautiful menu"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── POST INIT ────────────────────────────────────────────────────────────────
async def post_init(application: Application) -> None:
    await application.bot.delete_my_commands()
    await application.bot.set_my_commands([
        ("start", "Open Main Menu"),
        ("menu", "Show Menu"),
        ("signal", "Get Signal"),
        ("newtrade", "Record Trade"),
        ("closetrade", "Close Trade"),
        ("trades", "View Trades"),
        ("watch", "Start Alerts"),
    ])


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", show_menu))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("newtrade", cmd_newtrade))
    app.add_handler(CommandHandler("closetrade", cmd_closetrade))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(button_handler))

    log.info("🚀 Full Bot Started with Menu + Trade Journal + 30min Alerts")
    app.run_polling()


if __name__ == "__main__":
    main()
