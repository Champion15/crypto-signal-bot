import logging
from datetime import datetime
import os

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

COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana", "XRP": "ripple", "ADA": "cardano",
    "DOGE": "dogecoin", "TRX": "tron", "TON": "the-open-network",
    "MATIC": "matic-network", "DOT": "polkadot", "LTC": "litecoin",
    "AVAX": "avalanche-2", "LINK": "chainlink", "UNI": "uniswap",
    "ATOM": "cosmos", "XLM": "stellar", "INJ": "injective-protocol",
    "APT": "aptos", "ARB": "arbitrum", "OP": "optimism",
    "SUI": "sui", "SEI": "sei-network", "TIA": "celestia",
    "JUP": "jupiter-ag", "WIF": "dogwifcoin", "PEPE": "pepe",
    "SHIB": "shiba-inu", "FLOKI": "floki", "BONK": "bonk",
    "FET": "fetch-ai", "RENDER": "render-token", "GRT": "the-graph",
    "FIL": "filecoin", "ICP": "internet-computer", "HBAR": "hedera-hashgraph",
    "VET": "vechain", "ALGO": "algorand", "NEAR": "near",
    "FTM": "fantom", "SAND": "the-sandbox", "MANA": "decentraland",
    "AXS": "axie-infinity", "GALA": "gala", "ENJ": "enjincoin",
    "CHZ": "chiliz", "FLOW": "flow", "EGLD": "elrond-erd-2",
    "THETA": "theta-token", "EOS": "eos", "XTZ": "tezos",
    "NEO": "neo", "ZEC": "zcash", "DASH": "dash",
    "CAKE": "pancakeswap-token", "1INCH": "1inch",
}

INTERVAL_DAYS = {
    "1h": 7,
    "4h": 30,
    "1d": 180,
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)


def fetch_ohlcv(symbol, interval="1d"):
    coin_id = COINGECKO_IDS.get(symbol.upper())
    if not coin_id:
        supported = ", ".join(COINGECKO_IDS.keys())
        raise ValueError(f"Unknown symbol. Supported: {supported}")

    days = INTERVAL_DAYS.get(interval, 180)
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    response = requests.get(
        url,
        params={"vs_currency": "usd", "days": days},
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    log.info(f"CoinGecko returned {len(data)} candles for {coin_id} ({interval})")

    if not data or len(data) < 15:
        raise ValueError(f"Not enough data returned ({len(data) if data else 0} candles)")

    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close"])
    df["volume"] = 0.0
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("ts").set_index("ts")
    df.dropna(inplace=True)
    return df


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

    bb_squeeze = (bb_up - bb_low) / bb_mid < 0.1
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


async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage:\n/signal BTC\n/signal ETH 4h")
        return
    symbol = args[0].upper().replace("USDT", "")
    tf = args[1].lower() if len(args) > 1 else DEFAULT_TF
    if tf not in INTERVAL_DAYS:
        await update.message.reply_text("Supported timeframes: 1h, 4h, 1d")
        return
    await update.message.reply_text(f"⏳ Analysing {symbol}/USDT...")
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
        "/signal BTC\n"
        "/signal ETH 4h\n\n"
        "/watch BTC\n"
        "/unwatch BTC\n"
        "/list\n\n"
        "<b>Timeframes:</b> 1h, 4h, 1d\n\n"
        "<b>Supported coins:</b>\n"
        "BTC ETH BNB SOL XRP ADA\n"
        "DOGE TRX TON MATIC DOT LTC\n"
        "AVAX LINK UNI ATOM XLM INJ\n"
        "APT ARB OP SUI SEI TIA\n"
        "JUP WIF PEPE SHIB FLOKI BONK\n"
        "FET RENDER GRT FIL ICP HBAR\n"
        "VET ALGO NEAR FTM SAND MANA\n"
        "AXS GALA ENJ CHZ FLOW EGLD\n"
        "THETA EOS XTZ NEO ZEC DASH\n"
        "CAKE 1INCH"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
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
