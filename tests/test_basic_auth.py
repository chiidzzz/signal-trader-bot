# tests/test_basic_auth.py
import os
from dotenv import load_dotenv
from binance.client import Client

load_dotenv()

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")

print(f"API Key (first 10 chars): {api_key[:10]}...")
print(f"API Secret (first 10 chars): {api_secret[:10]}...")
print(f"API Key length: {len(api_key)}")
print(f"API Secret length: {len(api_secret)}")

try:
    client = Client(api_key, api_secret)
    
    # Test 1: Check server time (no auth needed)
    print("\n✅ Server time:", client.get_server_time())
    
    # Test 2: Check account status (requires auth)
    account = client.get_account()
    print("✅ Account status:", account['accountType'])
    print("✅ Can trade:", account['canTrade'])
    
except Exception as e:
    print(f"❌ Error: {e}")