#!/usr/bin/env python3
"""
Real-time listener to capture ANY Telegram chat_id
Send a message in the target group and it will show the ID instantly.
"""

import os
import sys
import asyncio
from pathlib import Path

from telethon import TelegramClient, events
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TG_API_ID"))
API_HASH = os.getenv("TG_API_HASH")

if not API_ID or not API_HASH:
    print("❌ TG_API_ID / TG_API_HASH missing from .env")
    sys.exit(1)

async def main():
    print("🔐 Connecting to Telegram...")

    client = TelegramClient("chat_id_listener_session", API_ID, API_HASH)
    await client.start()
    print("✅ Connected!")
    print("📡 Listening for any message you send...")

    @client.on(events.NewMessage)
    async def handler(event):
        chat = await event.get_chat()
        chat_id = event.chat_id

        print("\n=======================================")
        print("📍 MESSAGE RECEIVED")
        print("---------------------------------------")
        print(f"Chat Title : {getattr(chat, 'title', None)}")
        print(f"Chat ID    : {chat_id}")
        print("---------------------------------------")
        print("💡 Use this in your .env:")
        print(f"TG_NOTIFY_CHAT_ID={chat_id}")
        print("=======================================\n")

    print("💬 Now go send a message in your Telegram group (e.g., chadi-tester)")
    print("   Leave this window running...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
