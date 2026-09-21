"""
Signal — Catch-All Spam Triage Bot
------------------------------------
Built for KOLs/influencers drowning in DM spam. Monitors:
  1. Personal Telegram DMs (via Telethon, read-only)
  2. Messages sent to the connected bot (via python-telegram-bot)

Uses Llama 3.2 (via OpenRouter) to classify each incoming message as a
genuine opportunity (real collab, real partnership, real business lead)
versus the spam that floods a KOL's inbox (fake collab offers, phishing
links, giveaway/airdrop scams, templated bot-generated "let's partner"
pitches).

Genuine messages are:
  - Forwarded to the VIP Telegram channel
  - Logged to an in-app dashboard, viewable at /dashboard, with the AI's
    reasoning shown alongside each one
"""

import asyncio
import json
import logging
import os
import time

import requests
from aiohttp import web
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("triage")

# ---------------- Environment variables ----------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
VIP_CHANNEL_ID = os.environ["VIP_CHANNEL_ID"]

TELETHON_API_ID = int(os.environ["TELETHON_API_ID"])
TELETHON_API_HASH = os.environ["TELETHON_API_HASH"]
TELETHON_SESSION_STRING = os.environ["TELETHON_SESSION_STRING"]

PORT = int(os.environ.get("PORT", 10000))

# ---------------- In-memory + on-disk lead store ----------------
LEADS_FILE = "leads.json"
leads = []  # most recent first


def load_leads():
    global leads
    try:
        with open(LEADS_FILE, "r") as f:
            leads = json.load(f)
    except Exception:
        leads = []


def save_leads():
    try:
        with open(LEADS_FILE, "w") as f:
            json.dump(leads[:200], f)
    except Exception as e:
        logger.error(f"Could not save leads.json: {e}")


# ---------------- Llama 3.2 classification via OpenRouter ----------------
def classify(text: str) -> dict:
    prompt = (
        "You are a spam-triage assistant for a crypto/Web3 KOL or influencer whose "
        "DMs are flooded daily. Decide if this incoming message is a genuine "
        "opportunity worth their time, or spam/noise to ignore.\n\n"
        "Genuine opportunities: a real collab or sponsorship offer from an "
        "identifiable project, a real partnership proposal, a real business/client "
        "lead (e.g. a project wanting paid promotion, an OTC or market-making "
        "offer, an investor inquiry).\n\n"
        "Spam/noise: fake or templated collab offers, phishing links, fake "
        "support/wallet-verification messages, giveaway or airdrop scams, "
        "copy-pasted bot-generated \"let's partner\" pitches sent to many people, "
        "generic engagement farming, small talk.\n\n"
        f'Message:\n"""{text}"""\n\n'
        "Respond ONLY with strict JSON, no markdown formatting, in exactly this shape:\n"
        '{"important": true, "category": "collab_offer", "reason": "short sentence"}\n'
        'category must be one of: "collab_offer", "partnership", "client_lead", '
        '"scam_phishing", "spam", "other"'
    )
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            json={
                "model": "meta-llama/llama-3.2-3b-instruct",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=20,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Classification error: {e}")
        return {"important": False, "category": "other", "reason": "classification failed"}


# ---------------- Shared "important message" handling ----------------
async def handle_important(source: str, sender: str, text: str, result: dict, bot):
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "sender": sender,
        "text": text[:500],
        "category": result.get("category", "other"),
        "reason": result.get("reason", ""),
    }
    leads.insert(0, entry)
    save_leads()

    category_label = result.get("category", "lead").replace("_", " ").title()
    message = (
        f"Flagged: {category_label}\n"
        f"Source: {source}\n"
        f"From: {sender}\n\n"
        f"{text[:800]}\n\n"
        f"Why: {result.get('reason', '')}"
    )
    try:
        await bot.send_message(chat_id=VIP_CHANNEL_ID, text=message)
    except Exception as e:
        logger.error(f"Failed to forward to VIP channel: {e}")


# ---------------- Personal DM listener (Telethon) ----------------
telethon_client = TelegramClient(
    StringSession(TELETHON_SESSION_STRING), TELETHON_API_ID, TELETHON_API_HASH
)


def register_telethon_handler(bot):
    @telethon_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def personal_dm_handler(event):
        text = event.raw_text or ""
        if not text.strip():
            return
        sender = await event.get_sender()
        sender_name = (
            getattr(sender, "username", None)
            or getattr(sender, "first_name", None)
            or "Unknown"
        )
        result = classify(text)
        logger.info(f"[Personal DM] from {sender_name}: important={result.get('important')}")
        if result.get("important"):
            await handle_important("Personal DM", f"@{sender_name}", text, result, bot=bot)


# ---------------- Bot chat listener (python-telegram-bot) ----------------
async def bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Catch-All Triage Bot is running.")


async def bot_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if not text.strip():
        return
    user = update.effective_user
    sender = (user.username or user.first_name or "Unknown") if user else "Unknown"
    result = classify(text)
    logger.info(f"[Bot Chat] from {sender}: important={result.get('important')}")
    if result.get("important"):
        await handle_important("Bot Chat", f"@{sender}", text, result, bot=context.bot)


# ---------------- Dashboard (aiohttp) ----------------
async def dashboard(request):
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td><span class=\"tag {}\">{}</span></td>"
        "<td>{}</td><td class=\"reason\">{}</td></tr>".format(
            l["time"],
            l["source"],
            l["sender"],
            l["category"],
            l["category"].replace("_", " ").title(),
            l["text"],
            l.get("reason", ""),
        )
        for l in leads[:100]
    )
    html = f"""
    <html><head><title>Signal — Spam Triage Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body {{ font-family: sans-serif; background:#0d0d0d; color:#eee; padding:20px; }}
      table {{ width:100%; border-collapse: collapse; }}
      td, th {{ border:1px solid #2a2a2a; padding:8px; font-size:13px; text-align:left; vertical-align:top; }}
      th {{ background:#4b2e83; }}
      h1 {{ color:#a58fd6; margin-bottom:0; }}
      p.sub {{ color:#888; margin-top:4px; }}
      .reason {{ color:#a58fd6; font-style:italic; }}
      .tag {{ padding:2px 8px; border-radius:10px; font-size:11px; }}
      .tag.collab_offer, .tag.partnership, .tag.client_lead {{ background:#1f4d2b; color:#8fe0a3; }}
      .tag.scam_phishing {{ background:#5a1f1f; color:#f28b8b; }}
      .tag.spam, .tag.other {{ background:#333; color:#aaa; }}
    </style></head><body>
    <h1>Signal</h1>
    <p class="sub">Genuine messages flagged out of your inbox noise — {len(leads)} caught so far</p>
    <table>
      <tr><th>Time</th><th>Source</th><th>Sender</th><th>Category</th><th>Message</th><th>Why flagged</th></tr>
      {rows}
    </table>
    </body></html>
    """
    return web.Response(text=html, content_type="text/html")


async def health(request):
    return web.Response(text="Bot is alive")


# ---------------- Entry point ----------------
async def main():
    load_leads()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", bot_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot_message_handler))

    register_telethon_handler(application.bot)

    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/dashboard", dashboard)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Dashboard live on port {PORT}")

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot chat polling started.")

    await telethon_client.start()
    logger.info("Telethon personal-account listener started.")

    await telethon_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
    
