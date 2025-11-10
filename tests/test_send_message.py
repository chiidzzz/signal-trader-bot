#!/usr/bin/env python3
"""
Test sending a message to AntounNotifier
Place in tests/ folder and run: python test_send_message.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from telethon import TelegramClient
from dotenv import load_dotenv

load_dotenv()

async def main():
    """Test sending message to AntounNotifier."""
    
    api_id = int(os.getenv("TG_API_ID"))
    api_hash = os.getenv("TG_API_HASH")
    notify_chat_id = os.getenv("TG_NOTIFY_CHAT_ID")
    
    if not notify_chat_id:
        print("❌ TG_NOTIFY_CHAT_ID not found in .env")
        return
    
    print(f"🔐 Connecting to Telegram...")
    print(f"📱 Target chat ID: {notify_chat_id}")
    
    session_path = Path(__file__).parent.parent / "signals_session"  # Use SAME session as main bot
    client = TelegramClient(str(session_path), api_id, api_hash)
    
    await client.start()
    print("✅ Connected!\n")
    
    # Try to get the entity
    try:
        # First, try as integer
        try:
            chat_id = int(notify_chat_id)
        except ValueError:
            chat_id = notify_chat_id
        
        entity = await client.get_entity(chat_id)
        print(f"✅ Entity found: {entity}")
        print(f"   Type: {type(entity).__name__}")
        print(f"   ID: {entity.id}")
        if hasattr(entity, 'username'):
            print(f"   Username: @{entity.username}")
        if hasattr(entity, 'first_name'):
            print(f"   Name: {entity.first_name}")
        print()
        
        # Try sending a test message
        print("📤 Sending test message...")
        await client.send_message(entity, "🧪 **Test Message**\nThis is a test from your signals bot!")
        print("✅ Message sent successfully!")
        print("\n👉 Check 'AntounNotifier' to see if message arrived!")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    
    await client.disconnect()
    print("\n✨ Done!")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()