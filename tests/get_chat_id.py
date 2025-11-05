from telethon import TelegramClient
import os
from dotenv import load_dotenv

load_dotenv()

api_id = int(os.getenv("TG_API_ID"))
api_hash = os.getenv("TG_API_HASH")

client = TelegramClient("signals_session", api_id, api_hash)

async def main():
    async for dialog in client.iter_dialogs():
        print(f"{dialog.name}: {dialog.id}")

with client:
    client.loop.run_until_complete(main())
