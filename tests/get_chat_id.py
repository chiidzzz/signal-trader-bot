#!/usr/bin/env python3
"""
Script to get Telegram chat/channel IDs
Place in tests/ folder and run: python get_chat_id.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Add parent directory to path to import from project
sys.path.insert(0, str(Path(__file__).parent.parent))

from telethon import TelegramClient
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

async def main():
    """Fetch and display all accessible chats/channels with their IDs."""
    
    # Get credentials from .env
    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    
    if not api_id or not api_hash:
        print("❌ Error: TG_API_ID and TG_API_HASH not found in .env file")
        return
    
    # Create session in tests folder
    session_path = Path(__file__).parent / "get_chat_id_session"
    
    print("🔐 Connecting to Telegram...")
    client = TelegramClient(str(session_path), int(api_id), api_hash)
    
    await client.start()
    print("✅ Connected!\n")
    
    print("=" * 70)
    print("📋 YOUR TELEGRAM CHATS & CHANNELS")
    print("=" * 70)
    
    # Get all dialogs (chats, channels, groups)
    dialogs = await client.get_dialogs(limit=2000)
    
    channels = []
    groups = []
    users = []
    
    for dialog in dialogs:
        entity = dialog.entity
        
        # Determine type
        if hasattr(entity, 'broadcast'):
            if entity.broadcast:
                channels.append((dialog.name, entity.id))
            else:
                groups.append((dialog.name, entity.id))
        elif hasattr(entity, 'first_name'):
            users.append((dialog.name, entity.id))
    
    # Display Channels
    if channels:
        print("\n🔊 CHANNELS (broadcast only):")
        print("-" * 70)
        for name, chat_id in channels:
            print(f"   Name: {name}")
            print(f"   ID:   {chat_id}")
            print(f"   For .env use: TG_NOTIFY_CHAT_ID={chat_id}")
            print()
    
    # Display Groups
    if groups:
        print("\n👥 GROUPS (discussion groups):")
        print("-" * 70)
        for name, chat_id in groups:
            print(f"   Name: {name}")
            print(f"   ID:   {chat_id}")
            print(f"   For .env use: TG_NOTIFY_CHAT_ID={chat_id}")
            print()
    
    # Display Users (private chats)
    if users:
        print("\n💬 PRIVATE CHATS:")
        print("-" * 70)
        for name, chat_id in users[:50]:  # Show first 50 only
            print(f"   Name: {name}")
            print(f"   ID:   {chat_id}")
            print(f"   For .env use: TG_NOTIFY_CHAT_ID={chat_id}")
            print()
        
        if len(users) > 10:
            print(f"   ... and {len(users) - 50} more private chats")
    
    print("=" * 70)
    print("\n💡 TIPS:")
    print("   • For channels/groups, use the negative ID (it's already negative)")
    print("   • For private chats, use the positive ID")
    print("   • Copy the ID for TG_NOTIFY_CHAT_ID in your .env file")
    print("   • 'AntounNotifier' should be in the CHANNELS or GROUPS list above")
    print("\n✨ Done! Session saved in tests/get_chat_id_session.session")
    
    await client.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()