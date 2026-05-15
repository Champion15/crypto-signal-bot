import logging
import time
import os
from datetime import datetime

import pandas as pd
import pandas_ta as ta
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ── CONFIG ────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "8669805705:AAEeEawbQ5U5d-G2hJGV-fJjHO1r_1IVVJE")
WATCH_INTERVAL = 60 * 60
DEFAULT_TF = "1d"
SUPPORTED_TF = ["1m", "5m", "15m", "1h", "4h", "1d"]
OWNER_USERNAME = "@YourUsername"   # ← change this

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)


# ── KEYBOARDS ─────────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Scanning",    callback_data="menu_scanning"),
            InlineKeyboardButton("🔔 Alerts",      callback_data="menu_alerts"),
        ],
        [
            InlineKeyboardButton("📊 Performance", callback_data="menu_performance"),
            InlineKeyboardButton("ℹ️ Info",         callback_data="menu_info"),
        ],
        [
            InlineKeyboardButton("💡 Tips",        callback_data="menu_tips"),
            InlineKeyboardButton("❓ Help",         callback_data="menu_help"),
        ],
        [
            InlineKeyboardButton("📞 Contact Owner", callback_data="menu_contact"),
        ],
    ])


def scan_coin_keyboard():
    popular = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "PEPE", "ADA",
               "MATIC", "AVAX", "LINK", "ARB"]
    rows = []
    for i in range(0, len(popular), 3):
        row = [
            InlineKeyboardButton(popular[j], callback_data=f"scan_{popular[j]}")
            for j in range(i, min(i + 3, len(popular)))
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def tf_keyboard(symbol):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1m",  callback_data=f"sig_{symbol}_1m"),
            InlineKeyboardButton("5m",  callback_data=f"sig_{symbol}_5m"),
            InlineKeyboardButton("15m", callback_data=f"sig_{symbol}_15m"),
        ],
        [
            InlineKeyboardButton("1h",  callback_data=f"sig_{symbol}_1h"),
            InlineKeyboardButton("4h",  callback_data=f"sig_{symbol}_4h"),
            InlineKeyboardButton("1d",  callback_data=f"sig_{symbol}_1d"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="menu_scanning")],
    ])


def back_keyboard(dest="menu_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data=dest)]])


def result_keyboard(symbol, tf):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Re-scan",    callback_data=f"sig_{symbol}_{tf}"),
            InlineKeyboardButton("⏱ Change TF",  callback_data=f"scan_{symbol}"),
        ],
        [
            InlineKeyboardButton(f"👁 Watch {symbol}", callback_data=f"watch_{symbol}"),
            InlineKeyboardButton("🏠 Main Menu",        callback_data="menu_main"),
        ],
    ])


# ── DATA FETCHERS ─────────────────────────────────────────────────────────────

def fetch_kucoin(symbol, interval="1d"):
    tf_map = {"1m":"1min","5m":"5min","15m":"15min","1h":"1hour","4h":"4hour","1d":"1day"}
    r = requests.get(
        "https://api.kucoin.com/api/v1/market/candles",
        params={"symbol": f"{symbol.upper()}-USDT", "type": tf_map.get(interval, "1day")},
        timeout=15
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "200000":
        raise ValueError(f"KuCoin: {data.get('msg')}")
    candles = data["data"]
    if not candles or len(candles) < 15:
        raise ValueError(f"KuCoin: only {len(candles) if candles else 0} candles")
    df = pd.DataFrame(candles, columns=["ts","open","close","high","low","volume","turnover"])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="s")
    return df.sort_values("ts").set_index("ts").dropna()


def fetch_mexc(symbol, interval="1d"):
    tf_map = {"1m":"1m","5m":"5m","15m":"15m","1h":"1h","4h":"4h","1d":"1d"}
    r = requests.get(
        "https://api.mexc.com/api/v3/klines",
        params={"symbol": f"{symbol.upper()}USDT", "interval": tf_map.get(interval,"1d"), "limit": 200},
        timeout=15
    )
    r.raise_for_status()
    candles = r.json()
    if not candles or len(candles) < 15:
        raise ValueError(f"MEXC: only {len(candles) if candles else 0} candles")
    df = pd.DataFrame(candles, columns=[
        "ts","open","high","low","close","volume",
        "close_ts","quote_vol","trades","taker_buy_base","taker_buy_quote","ignore"
    ])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df.sort_values("ts").set_index("ts").dropna()


def fetch_gate(symbol, interval="1d"):
    tf_map = {"1m":"1m","5m":"5m","15m":"15m","1h":"1h","4h":"4h","1d":"1d"}
    r = requests.get(
        "https://api.gateio.ws/api/v4/spot/candlesticks",
        params={"currency_pair": f"{symbol.upper()}_USDT",
                "interval": tf_map.get(interval,"1d"), "limit": 200},
        timeout=15
    )
    r.raise_for_status()
    candles = r.json()
    if not candles or len(candles) < 15:
        raise ValueError(f"Gate.io: only {len(candles) if candles else 0} candles")
    df = pd.DataFrame(candles, columns=["ts","volume","close","high","low","open","base_volume","is_closed"])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="s")
    return df.sort_values("ts").set_index("ts").dropna()


def fetch_okx(symbol, interval="1d"):
    tf_map = {"1m":"1m","5m":"5m","15m":"15m","1h":"1H","4h":"4H","1d":"1D"}
    r = requests.get(
        "https://www.okx.com/api/v5/market/candles",
        params={"instId": f"{symbol.upper()}-USDT",
                "bar": tf_map.get(interval,"1D"), "limit": 200},
        timeout=15
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise ValueError(f"OKX: {data.get('msg')}")
    candles = data["data"]
    if not candles or len(candles) < 15:
        raise ValueError(f"OKX: only {len(candles) if candles else 0} candles")
    df = pd.DataFrame(candles, columns=["ts","open","high","low","close","vol","volCcy","volCcyQuote","confirm"])
    for c in ["open","high","low","close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms")
    df["volume"] = pd.to_numeric(df["vol"], errors="coerce")
    return df.sort_values("ts").set_index("ts").dropna(subset=["open","high","low","close"])


def fetch_ohlcv(symbol, interval="1d"):
    errors = []
    for name, fn in [("KuCoin",fetch_kucoin),("MEXC",fetch_mexc),
                     ("Gate.io",fetch_gate),("OKX",fetch_okx)]:
        try:
            df = fn(symbol, interval)
            log.info(f"{name}: {len(df)} candles for {symbol} ({interval})")
            return df
        except Exception as e:
            log.warning(f"{name} failed: {e}")
            errors.append(f"{name}: {e}")
            time.sleep(0.5)
    raise ValueError("All sources failed:\n" + "\n".join(errors))


# ── ANALYSIS ──────────────────────────────────────────────────────────────────

def analyse(df):
    df = df.copy()
    close, high, low = df["close"], df["high"], df["low"]

    df["ema20"]  = ta.ema(close, length=10)
    df["ema50"]  = ta.ema(close, length=20)
    df["ema200"] = ta.ema(close, length=35)
    df["rsi"]    = ta.rsi(close, length=7)
    df["atr"]    = ta.atr(high, low, close, length=7)

    macd = ta.macd(close, fast=6, slow=13, signal=5)
    if macd is None or macd.empty:
        raise ValueError("MACD calculation failed")
    df["macd"]      = macd["MACD_6_13_5"]
    df["macd_sig"]  = macd["MACDs_6_13_5"]
    df["macd_hist"] = macd["MACDh_6_13_5"]

    bb = ta.bbands(close, length=10)
    if bb is None or bb.empty:
        raise ValueError("Bollinger Bands calculation failed")
    cols = bb.columns.tolist()
    df["bb_low"] = bb[cols[0]]
    df["bb_mid"] = bb[cols[1]]
    df["bb_up"]  = bb[cols[2]]

    df.dropna(inplace=True)
    if len(df) < 5:
        raise ValueError("Not enough candles after indicator calculation")

    r = df.iloc[-1]
    price  = float(r["close"])
    atr    = float(r["atr"])
    rsi    = float(r["rsi"])
    ema20  = float(r["ema20"])
    ema50  = float(r["ema50"])
    ema200 = float(r["ema200"])
    macd_v = float(r["macd"])
    macd_s = float(r["macd_sig"])
    bb_up  = float(r["bb_up"])
    bb_low = float(r["bb_low"])

    score = 0
    if   ema20 > ema50 > ema200: score += 2
    elif ema20 < ema50 < ema200: score -= 2

    if   macd_v > macd_s and macd_v > 0: score += 2
    elif macd_v > macd_s:                score += 1
    elif macd_v < macd_s and macd_v < 0: score -= 2
    elif macd_v < macd_s:                score -= 1

    if   50 < rsi < 75 and ema20 > ema50: score += 1
    elif rsi < 25:                         score += 1
    elif rsi < 50 and ema20 < ema50:       score -= 1
    elif rsi > 75:                         score -= 1

    if   price > ema20 > ema50: score += 1
    elif price < ema20 < ema50: score -= 1

    if   price < bb_low * 1.02 and ema20 > ema50: score += 1
    elif price > bb_up  * 0.98 and ema20 < ema50: score -= 1

    confidence = round((abs(score) / 7) * 100)

    if   score >= 4:  direction = "LONG 📈"
    elif score <= -4: direction = "SHORT 📉"
    else:             direction = "NEUTRAL ⚖️"

    entry = sl = tp1 = tp2 = tp3 = None
    rr = 0
    if "LONG" in direction:
        entry = round(price,   4)
        sl    = round(price - 1.5 * atr, 4)
        tp1   = round(price + 1.5 * atr, 4)
        tp2   = round(price + 3.0 * atr, 4)
        tp3   = round(price + 5.0 * atr, 4)
        rr    = round(abs(tp2 - entry) / abs(sl - entry), 2) if sl != entry else 0
    elif "SHORT" in direction:
        entry = round(price,   4)
        sl    = round(price + 1.5 * atr, 4)
        tp1   = round(price - 1.5 * atr, 4)
        tp2   = round(price - 3.0 * atr, 4)
        tp3   = round(price - 5.0 * atr, 4)
        rr    = round(abs(tp2 - entry) / abs(sl - entry), 2) if sl != entry else 0

    return {
        "price": round(price, 4), "direction": direction,
        "score": score, "confidence": confidence,
        "rsi": round(rsi, 2), "atr": round(atr, 6),
        "ema20": round(ema20, 4), "ema50": round(ema50, 4),
        "entry": entry, "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr,
        "macd_hist": round(float(r["macd_hist"]), 6),
        "bb_low": round(bb_low, 4), "bb_up": round(bb_up, 4),
    }


# ── FORMAT SIGNAL ─────────────────────────────────────────────────────────────

def format_signal(symbol, tf, sig):
    stars = "⭐" * abs(sig["score"]) or "—"
    bar   = "🟩" * (sig["confidence"] // 20) + "⬜" * (5 - sig["confidence"] // 20)
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>{symbol}/USDT</b> | <b>{tf}</b>\n"
        f"🕒 {now}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>Signal:</b> {sig['direction']}  {stars}\n"
        f"🎯 <b>Confidence:</b> {sig['confidence']}%  {bar}\n\n"
        "📈 <b>Indicators</b>\n"
        f"  RSI       : {sig['rsi']}\n"
        f"  MACD Hist : {sig['macd_hist']}\n"
        f"  EMA Short : {sig['ema20']}\n"
        f"  EMA Mid   : {sig['ema50']}\n"
        f"  ATR       : {sig['atr']}\n"
        f"  BB Low    : {sig['bb_low']}\n"
        f"  BB Up     : {sig['bb_up']}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    if sig["entry"]:
        text += (
            "📋 <b>Trade Setup</b>\n\n"
            f"  🎯 Entry      : <b>{sig['entry']}</b>\n"
            f"  🛑 Stop Loss  : <b>{sig['sl']}</b>\n\n"
            f"  ✅ TP1        : {sig['tp1']}\n"
            f"  ✅ TP2        : {sig['tp2']}\n"
            f"  ✅ TP3        : {sig['tp3']}\n\n"
            f"  📐 Risk/Reward: <b>1:{sig['rr']}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>⚠️ Not financial advice. DYOR.</i>"
        )
    else:
        text += (
            "⚠️ <b>No strong setup yet.</b>\n"
            "Wait for confirmation before entering.\n\n"
            "━━━━━━━━━━━━━━━━━━"
        )
    return text


# ── MENU PAGE CONTENT ─────────────────────────────────────────────────────────

MENU_PAGES = {
    "menu_alerts": (
        "🔔 <b>Alerts</b>\n\n"
        "Auto-alerts fire every hour when a strong signal (score ≥ 4) is detected.\n\n"
        "<b>Commands:</b>\n"
        "  /watch BTC — Start hourly alerts for BTC\n"
        "  /unwatch BTC — Stop alerts\n"
        "  /list — View your active watchlist\n\n"
        "<i>You can watch multiple coins at once.</i>"
    ),
    "menu_performance": (
        "📊 <b>Performance</b>\n\n"
        "Multi-indicator scoring system (max 7 pts):\n\n"
        "  📌 EMA Stack (10 / 20 / 35)  → ±2 pts\n"
        "  📌 MACD (6 / 13 / 5)         → ±2 pts\n"
        "  📌 RSI (7)                    → ±1 pt\n"
        "  📌 Price vs EMA               → ±1 pt\n"
        "  📌 Bollinger Bands (10)       → ±1 pt\n\n"
        "  🟢 Score ≥ +4  →  LONG\n"
        "  🔴 Score ≤ −4  →  SHORT\n"
        "  ⚪ Otherwise   →  NEUTRAL\n\n"
        "ATR (7) is used to calculate SL & TP levels."
    ),
    "menu_info": (
        "ℹ️ <b>Bot Info</b>\n\n"
        "🤖 Crypto Signal Bot\n\n"
        "📡 <b>Data sources</b> (tried in order):\n"
        "  1. KuCoin\n"
        "  2. MEXC\n"
        "  3. Gate.io\n"
        "  4. OKX\n\n"
        "⏱ <b>Timeframes:</b> 1m · 5m · 15m · 1h · 4h · 1d\n"
        "💱 Works with <b>ANY USDT pair</b>\n\n"
        "<i>Built for serious traders 📈</i>"
    ),
    "menu_tips": (
        "💡 <b>Trading Tips</b>\n\n"
        "1️⃣  Higher timeframes (4h, 1d) = more reliable signals\n\n"
        "2️⃣  Always confirm with your own analysis\n\n"
        "3️⃣  Use TP1 to take partial profits early\n\n"
        "4️⃣  Never risk more than you can afford to lose\n\n"
        "5️⃣  NEUTRAL = <b>sit on your hands</b> 🙌\n\n"
        "6️⃣  ATR-based SL is your friend — respect it\n\n"
        "7️⃣  Combine with volume & market structure for best results"
    ),
    "menu_help": (
        "❓ <b>Help</b>\n\n"
        "<b>Commands:</b>\n"
        "  /start — Show main menu\n"
        "  /signal BTC — Signal (default: 1d)\n"
        "  /signal ETH 15m — Signal on 15m\n"
        "  /watch BTC — Auto-alerts every hour\n"
        "  /unwatch BTC — Stop alerts\n"
        "  /list — Show watched coins\n\n"
        "<b>Or just tap Scanning in the menu</b>\n"
        "and pick any coin + timeframe with buttons!\n\n"
        "Works with: BTC · ETH · SOL · DOGE · PEPE · any USDT pair"
    ),
}


# ── CALLBACK HANDLER (all button taps) ───────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()   # removes loading spinner immediately
    data  = query.data

    # Main menu
    if data == "menu_main":
        await query.edit_message_text(
            "What would you like to do? Tap a category below 👇",
            reply_markup=main_menu_keyboard()
        )
        return

    # Scanning page
    if data == "menu_scanning":
        await query.edit_message_text(
            "🔍 <b>Scanning</b>\n\nChoose a coin to analyse:",
            parse_mode=ParseMode.HTML,
            reply_markup=scan_coin_keyboard()
        )
        return

    # Static info pages
    if data in MENU_PAGES:
        await query.edit_message_text(
            MENU_PAGES[data],
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard()
        )
        return

    # Contact owner
    if data == "menu_contact":
        await query.edit_message_text(
            f"📞 <b>Contact Owner</b>\n\n"
            f"For support, feedback, or custom bots:\n\n"
            f"👤 {OWNER_USERNAME}\n\n"
            "<i>⚠️ This bot is for educational purposes only. Not financial advice.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard()
        )
        return

    # Coin selected → show timeframe picker
    if data.startswith("scan_"):
        symbol = data[5:]
        await query.edit_message_text(
            f"📈 <b>{symbol}/USDT</b>\n\nSelect a timeframe:",
            parse_mode=ParseMode.HTML,
            reply_markup=tf_keyboard(symbol)
        )
        return

    # Watch coin via button
    if data.startswith("watch_"):
        symbol   = data[6:]
        chat_id  = query.message.chat_id
        job_name = f"{chat_id}_{symbol}"
        if ctx.job_queue.get_jobs_by_name(job_name):
            await query.answer(f"Already watching {symbol}!", show_alert=True)
            return
        ctx.job_queue.run_repeating(
            auto_signal, interval=WATCH_INTERVAL, first=10,
            chat_id=chat_id, name=job_name,
            data={"symbol": symbol, "tf": DEFAULT_TF}
        )
        await query.edit_message_text(
            f"👁 Now watching <b>{symbol}/USDT</b>\n\n"
            "You'll receive alerts every hour when a strong signal fires.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard()
        )
        return

    # Signal: coin + timeframe → fetch and display
    if data.startswith("sig_"):
        parts  = data.split("_")   # ["sig", "BTC", "1d"]
        symbol = parts[1]
        tf     = parts[2]
        await query.edit_message_text(f"⏳ Fetching {symbol}/USDT · {tf}…")
        try:
            df     = fetch_ohlcv(symbol, tf)
            signal = analyse(df)
            text   = format_signal(symbol, tf, signal)
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=result_keyboard(symbol, tf)
            )
        except Exception as e:
            log.error(f"Signal error: {e}")
            await query.edit_message_text(
                f"❌ <b>Error fetching {symbol}/USDT ({tf})</b>\n\n<code>{e}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard("menu_scanning")
            )
        return

    # Unknown callback fallback
    log.warning(f"Unknown callback: {data}")
    await query.answer("Unknown action", show_alert=True)


# ── TEXT MESSAGE HANDLER ──────────────────────────────────────────────────────
# Typing a coin ticker (e.g. "BTC") triggers the timeframe picker

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    text = text.replace("USDT","").replace("/","").replace("-","")
    if 2 <= len(text) <= 10 and text.isalnum():
        await update.message.reply_text(
            f"📈 <b>{text}/USDT</b>\n\nSelect a timeframe:",
            parse_mode=ParseMode.HTML,
            reply_markup=tf_keyboard(text)
        )
    else:
        await update.message.reply_text(
            "Tap a category or type a coin symbol (e.g. BTC, ETH, SOL):",
            reply_markup=main_menu_keyboard()
        )


# ── AUTO SIGNAL JOB ───────────────────────────────────────────────────────────

async def auto_signal(ctx: ContextTypes.DEFAULT_TYPE):
    job    = ctx.job
    symbol = job.data["symbol"]
    tf     = job.data["tf"]
    try:
        df     = fetch_ohlcv(symbol, tf)
        signal = analyse(df)
        if abs(signal["score"]) >= 4:
            text = "🔔 <b>AUTO ALERT</b>\n\n" + format_signal(symbol, tf, signal)
            await ctx.bot.send_message(
                chat_id=job.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=result_keyboard(symbol, tf)
            )
    except Exception as e:
        log.error(f"Auto signal error ({symbol}): {e}")


# ── COMMAND HANDLERS ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "What would you like to do? Tap a category below 👇",
        reply_markup=main_menu_keyboard()
    )


async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /signal BTC  or  /signal ETH 15m")
        return
    symbol = args[0].upper().replace("USDT", "")
    tf     = args[1].lower() if len(args) > 1 else DEFAULT_TF
    if tf not in SUPPORTED_TF:
        await update.message.reply_text(f"❌ Invalid timeframe. Choose: {', '.join(SUPPORTED_TF)}")
        return
    msg = await update.message.reply_text(f"⏳ Fetching {symbol}/USDT · {tf}…")
    try:
        df     = fetch_ohlcv(symbol, tf)
        signal = analyse(df)
        text   = format_signal(symbol, tf, signal)
        await msg.edit_text(text, parse_mode=ParseMode.HTML,
                            reply_markup=result_keyboard(symbol, tf))
    except Exception as e:
        log.error(e)
        await msg.edit_text(f"❌ <b>Error</b>\n<code>{e}</code>",
                            parse_mode=ParseMode.HTML,
                            reply_markup=back_keyboard())


async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /watch BTC")
        return
    symbol   = ctx.args[0].upper().replace("USDT", "")
    chat_id  = update.effective_chat.id
    job_name = f"{chat_id}_{symbol}"
    if ctx.job_queue.get_jobs_by_name(job_name):
        await update.message.reply_text(f"Already watching {symbol}/USDT.")
        return
    ctx.job_queue.run_repeating(
        auto_signal, interval=WATCH_INTERVAL, first=10,
        chat_id=chat_id, name=job_name,
        data={"symbol": symbol, "tf": DEFAULT_TF}
    )
    await update.message.reply_text(
        f"👁 Watching <b>{symbol}/USDT</b> — alerts every hour.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard()
    )


async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /unwatch BTC")
        return
    symbol   = ctx.args[0].upper().replace("USDT", "")
    chat_id  = update.effective_chat.id
    job_name = f"{chat_id}_{symbol}"
    jobs = ctx.job_queue.get_jobs_by_name(job_name)
    if not jobs:
        await update.message.reply_text(f"{symbol} is not being watched.")
        return
    for job in jobs:
        job.schedule_removal()
    await update.message.reply_text(
        f"🔕 Stopped watching <b>{symbol}/USDT</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard()
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    watched = [
        j.name.replace(f"{chat_id}_", "")
        for j in ctx.job_queue.jobs()
        if j.name and j.name.startswith(str(chat_id))
    ]
    if watched:
        await update.message.reply_text(
            "👁 <b>Active watchlist:</b>\n" + "\n".join(f"  • {w}" for w in watched),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "No coins in your watchlist.\nUse /watch BTC to add one.",
            reply_markup=main_menu_keyboard()
        )


# ── POST INIT ─────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    # Clear any old/broken commands first, then set the correct ones
    await app.bot.delete_my_commands()
    await app.bot.set_my_commands([
        ("start",   "Show main menu"),
        ("signal",  "Get signal  e.g. /signal BTC 15m"),
        ("watch",   "Auto-alerts e.g. /watch BTC"),
        ("unwatch", "Stop alerts e.g. /unwatch BTC"),
        ("list",    "Show watched coins"),
    ])
    log.info("Bot commands set ✅")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("signal",  cmd_signal))
    app.add_handler(CommandHandler("watch",   cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("list",    cmd_list))

    # All inline-button taps
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Plain-text coin lookup (e.g. user types "SOL")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot started ✅")
    app.run_polling()


if __name__ == "__main__":
    main()
