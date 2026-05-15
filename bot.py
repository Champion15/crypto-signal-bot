import logging
import time
import os
from datetime import datetime

import pandas as pd
import pandas_ta as ta
import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# CONFIG
BOT_TOKEN = os.getenv("BOT_TOKEN", "8669805705:AAEeEawbQ5U5d-G2hJGV-fJjHO1r_1IVVJE")
WATCH_INTERVAL = 60 * 60
DEFAULT_TF = "1d"

SUPPORTED_TF = ["1m", "5m", "15m", "1h", "4h", "1d"]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)


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


# ── MAIN FETCH — tries all sources automatically ──────────────────────────────

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
    if macd is None or macd.empty:
        raise ValueError("MACD calculation failed")
    df["macd"] = macd["MACD_6_13_5"]
    df["macd_signal"] = macd["MACDs_6_13_5"]
    df["macd_hist"] = macd["MACDh_6_13_5"]

    bb = ta.bbands(close, length=10)
    if bb is None or bb.empty:
        raise ValueError("Bollinger Band calculation failed")
    bb_cols = bb.columns.tolist()
    df["bb_low"] = bb[bb_cols[0]]
    df["bb_mid"] = bb[bb_cols[1]]
    df["bb_up"] = bb[bb_cols[2]]

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
    macd_val = float(latest["macd"])
    macd_sig = float(latest["macd_signal"])
    bb_up = float(latest["bb_up"])
    bb_low = float(latest["bb_low"])
    bb_mid = float(latest["bb_mid"])

    bullish_ema = ema20 > ema50 > ema200
    bearish_ema = ema20 < ema50 < ema200
    macd_bull = macd_val > macd_sig
    macd_bear = macd_val < macd_sig
    price_above_ema = price > ema20 > ema50
    price_below_ema = price < ema20 < ema50
    rsi_bullish = 50 < rsi < 75
    rsi_bearish = rsi < 50
    rsi_overbought = rsi > 75
    rsi_oversold = rsi < 25
    price_near_bb_low = price < bb_low * 1.02
    price_near_bb_up = price > bb_up * 0.98

    score = 0

    if bullish_ema:
        score += 2
    elif bearish_ema:
        score -= 2

    if macd_bull and macd_val > 0:
        score += 2
    elif macd_bull:
        score += 1
    elif macd_bear and macd_val < 0:
        score -= 2
    elif macd_bear:
        score -= 1

    if rsi_bullish and bullish_ema:
        score += 1
    elif rsi_oversold:
        score += 1
    elif rsi_bearish and bearish_ema:
        score -= 1
    elif rsi_overbought:
        score -= 1

    if price_above_ema:
        score += 1
    elif price_below_ema:
        score -= 1

    if price_near_bb_low and bullish_ema:
        score += 1
    elif price_near_bb_up and bearish_ema:
        score -= 1

    max_score = 7
    confidence = round((abs(score) / max_score) * 100)

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
        "macd_hist": round(float(latest["macd_hist"]), 4),
        "bb_low": round(bb_low, 4),
        "bb_up": round(bb_up, 4),
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
        f"MACD Hist: {sig['macd_hist']}\n"
        f"EMA Short: {sig['ema20']}\n"
        f"EMA Mid: {sig['ema50']}\n"
        f"ATR: {sig['atr']}\n"
        f"BB Low: {sig['bb_low']}\n"
        f"BB Up: {sig['bb_up']}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    if sig["entry"]:
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
    else:
        text += (
            "⚠️ No strong setup yet.\n"
            "Wait for confirmation.\n\n"
            "━━━━━━━━━━━━━━━━━━"
        )

    return text


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


async def auto_signal(ctx: ContextTypes.DEFAULT_TYPE):
    job = ctx.job
    symbol = job.data["symbol"]
    tf = job.data["tf"]
    try:
        df = fetch_ohlcv(symbol, tf)
        signal = analyse(df)
        if abs(signal["score"]) >= 4:
            text = "🔔 <b>AUTO ALERT</b>\n\n" + format_signal(symbol, tf, signal)
            await ctx.bot.send_message(
                chat_id=job.chat_id,
                text=text,
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        log.error(f"Auto signal error: {e}")


async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage:\n/watch BTC")
        return
    symbol = ctx.args[0].upper().replace("USDT", "")
    chat_id = update.effective_chat.id
    job_name = f"{chat_id}_{symbol}"
    if ctx.job_queue.get_jobs_by_name(job_name):
        await update.message.reply_text(f"Already watching {symbol}")
        return
    ctx.job_queue.run_repeating(
        auto_signal, interval=WATCH_INTERVAL, first=10,
        chat_id=chat_id, name=job_name,
        data={"symbol": symbol, "tf": DEFAULT_TF}
    )
    await update.message.reply_text(f"👁 Watching {symbol}/USDT")


async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage:\n/unwatch BTC")
        return
    symbol = ctx.args[0].upper().replace("USDT", "")
    chat_id = update.effective_chat.id
    job_name = f"{chat_id}_{symbol}"
    jobs = ctx.job_queue.get_jobs_by_name(job_name)
    if not jobs:
        await update.message.reply_text(f"{symbol} not being watched")
        return
    for job in jobs:
        job.schedule_removal()
    await update.message.reply_text(f"🔕 Stopped watching {symbol}")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    watched = [
        job.name.replace(f"{chat_id}_", "")
        for job in ctx.job_queue.jobs()
        if job.name and job.name.startswith(str(chat_id))
    ]
    if watched:
        await update.message.reply_text("👁 Watching:\n" + "\n".join(watched))
    else:
        await update.message.reply_text("No active watchlist")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>Crypto Signal Bot</b>\n\n"
        "<b>Commands:</b>\n"
        "/signal BTC — Get signal for BTC\n"
        "/signal ETH 15m — Get signal on 15m timeframe\n"
        "/watch BTC — Auto-alerts every hour\n"
        "/unwatch BTC — Stop alerts\n"
        "/list — Show watched coins\n\n"
        "<b>Timeframes:</b>\n"
        "1m, 5m, 15m, 1h, 4h, 1d\n\n"
        "<b>Works with ANY coin!</b>\n"
        "Just type the symbol:\n"
        "BTC, ETH, SOL, DOGE, PEPE, etc."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── POST INIT (Set Bot Commands) ─────────────────────────────────────────────

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        ("start", "Start the bot"),
        ("help", "Show help menu"),
        ("signal", "Get trading signal (e.g. /signal BTC 15m)"),
        ("watch", "Watch a coin for alerts (e.g. /watch BTC)"),
        ("unwatch", "Stop watching a coin"),
        ("list", "Show watched coins"),
    ])


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("list", cmd_list))
    log.info("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
