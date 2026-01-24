import requests
from typing import Optional
import time

class MarketCapChecker:
    def __init__(self):
        self.coingecko_url = "https://api.coingecko.com/api/v3"
        self.cache = {}  # Simple cache to avoid rate limits
        self.cache_ttl = 3600  # Cache for 1 hour
        
    def get_market_cap(self, symbol: str) -> Optional[float]:
        """
        Fetch market cap for a token symbol.
        Returns market cap in USD or None if not found.
        """
        symbol = symbol.upper().replace("/", "").replace("USDT", "").replace("USDC", "").replace("BUSD", "")
        
        # Check cache first
        if symbol in self.cache:
            cached_data, timestamp = self.cache[symbol]
            if time.time() - timestamp < self.cache_ttl:
                return cached_data
        
        try:
            # CoinGecko API (free, no key needed)
            # Search for coin by symbol
            search_url = f"{self.coingecko_url}/search?query={symbol}"
            resp = requests.get(search_url, timeout=10)
            
            if resp.status_code != 200:
                print(f"[MARKET_CAP] API error: {resp.status_code}")
                return None
            
            coins = resp.json().get("coins", [])
            if not coins:
                print(f"[MARKET_CAP] No data found for {symbol}")
                return None
            
            # Get the first match (most relevant by market cap)
            coin_id = coins[0]["id"]
            
            # Fetch market cap
            detail_url = f"{self.coingecko_url}/coins/{coin_id}?localization=false&tickers=false&community_data=false&developer_data=false"
            detail_resp = requests.get(detail_url, timeout=10)
            
            if detail_resp.status_code != 200:
                return None
            
            data = detail_resp.json()
            market_cap = data.get("market_data", {}).get("market_cap", {}).get("usd")
            
            if market_cap:
                # Cache it
                self.cache[symbol] = (float(market_cap), time.time())
                print(f"[MARKET_CAP] {symbol}: ${market_cap:,.0f}")
                return float(market_cap)
            
        except Exception as e:
            print(f"[MARKET_CAP] Error fetching {symbol}: {e}")
        
        return None
    
    def check_filter(self, symbol: str, min_cap: float, max_cap: float) -> tuple[bool, Optional[float]]:
        """
        Check if symbol passes market cap filter.
        Returns (passes_filter: bool, market_cap: float|None)
        """
        market_cap = self.get_market_cap(symbol)
        
        if market_cap is None:
            # Cannot verify - conservative: skip if can't verify
            return False, None
        
        passes = True
        
        if min_cap > 0 and market_cap < min_cap:
            passes = False
        
        if max_cap > 0 and market_cap > max_cap:
            passes = False
        
        return passes, market_cap
