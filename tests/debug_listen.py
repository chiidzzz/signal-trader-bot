# tests/debug_listen.py
import os
from telethon import TelegramClient, events
from dotenv import load_dotenv

load_dotenv()

api_id = int(os.getenv("TG_API_ID"))
api_hash = os.getenv("TG_API_HASH")
session = "signals_session"
channel_id = int(os.getenv("TG_CHANNEL_ID_OR_USERNAME"))

client = TelegramClient(session, api_id, api_hash)

@client.on(events.NewMessage(chats=channel_id))
async def handler(event):
    chat = await event.get_chat()
    print(f"[DEBUG] Message from {getattr(chat, 'title', chat)} ({chat.id}): {event.raw_text[:120]!r}")

print("✅ Listening for messages...  Send something in your group.")
client.start()
client.run_until_disconnected()
