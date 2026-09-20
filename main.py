import os
import json
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Fetch environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
VIP_CHANNEL_ID = os.getenv("VIP_CHANNEL_ID")

def analyze_deal_with_ai(text: str) -> dict:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    
    prompt = (
        "You are an AI Web3 Deal Triage Assistant. Analyze the incoming message and determine if it contains a business deal, "
        "partnership inquiry, sponsorship pitch, advisory proposal, or high-value offer. "
        "Respond ONLY with a valid JSON object matching this exact schema without any extra text or markdown code blocks:\n"
        '{"is_business": boolean, "budget": number, "category": "string", "summary": "string"}'
    )

    payload = {
        "model": "meta-llama/llama-3.2-3b-instruct:free",
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text}
        ],
        "temperature": 0.1
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=12)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
        
        # Clean potential markdown formatting if returned
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("
