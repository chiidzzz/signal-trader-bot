from telethon import TelegramClient
import os
from dotenv import load_dotenv

load_dotenv()

api_id = int(os.getenv("TG_API_ID"))
api_hash = os.getenv("TG_API_HASH")

client = TelegramClient("signals_session", api_id, api_hash)

async def main():
    print("🔍 Checking accessible groups...")
    found = False
    async for dialog in client.iter_dialogs():
        if "wled" in dialog.name.lower() or "khorrom" in dialog.name.lower():
            print(f"✅ Found group: {dialog.name} → ID: {dialog.id}")
            found = True
    if not found:
        print("❌ 'Wled khorrom bek' not found in your current session.")
        print("➡ Make sure you're logged in with the same Telegram account that joined the group.")

with client:
    client.loop.run_until_complete(main())
