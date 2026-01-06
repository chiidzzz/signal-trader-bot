import os
import json
from typing import Optional
from groq import Groq
from parsers.signal_parser import ParsedSignal, TPSet

class AISignalParser:
    def __init__(self):
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY not found in environment variables")
        self.client = Groq(api_key=api_key)
        self.model = "llama-3.3-70b-versatile"  # Latest 70B model (recommended)
    
    def parse(self, text: str) -> Optional[ParsedSignal]:
        """
        Parse trading signal using Groq AI
        Returns ParsedSignal or None if parsing fails
        """
        try:
            prompt = f"""Analyze this message and determine if it contains a REAL trading signal with specific buy/sell instructions.

If this is NOT a trading signal (just news, commentary, analysis, price movements), return: {{"is_signal": false}}

If this IS a trading signal with clear entry/exit prices, extract the data.

IMPORTANT: Only parse messages that explicitly tell you to BUY or SELL at specific prices.
DO NOT create trading signals from:
- Market news or price drop announcements
- Liquidation reports
- General market commentary
- Price predictions without clear buy instructions

Required JSON format:
{{
  "is_signal": boolean (true only if explicit buy/sell instruction exists),
  "coin_pair": "string (ticker/USDC, e.g., BTC/USDC)",
  "entry_price": number,
  "stop_loss": number or null,
  "tp1": number or null,
  "tp2": number or null,
  "tp3": number or null,
  "capital_allocation": number or null,
  "time_horizon_days": number or null,
  "spot_only": boolean
}}

Message:
{text}"""

            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a cryptocurrency trading signal parser. Extract structured data and return only valid JSON. CRITICAL: Use real, valid cryptocurrency ticker symbols (e.g., OP for Optimism, DOT for Polkadot, BTC for Bitcoin). Do not invent abbreviations - use the actual ticker traded on exchanges."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.1,  # Low temperature for consistent output
                max_tokens=300,
                response_format={"type": "json_object"}  # Force JSON output
            )
            
            # Parse response
            result = json.loads(response.choices[0].message.content)

            # Check if it's actually a signal
            if not result.get("is_signal", False):
                print("AI determined this is NOT a trading signal")
                return None

            # Convert to ParsedSignal format
            return self._convert_to_parsed_signal(text, result)
            
        except Exception as e:
            print(f"AI Parser Error: {e}")
            return None
    
    def _convert_to_parsed_signal(self, raw_text: str, ai_result: dict) -> Optional[ParsedSignal]:
        """Convert AI JSON result to ParsedSignal object"""
        try:
            # Extract coin pair and enforce USDC pairing
            coin_pair = ai_result.get("coin_pair", "")

            # If it's a pair like "XRP/USDT", extract base currency
            if "/" in coin_pair:
                base = coin_pair.split("/")[0]
                currency_display = f"{base}/USDC"  # Force USDC
                symbol_hint = f"{base}USDC"
            else:
                # If no slash, assume it's just the base and add USDC
                currency_display = f"{coin_pair}/USDC"
                symbol_hint = f"{coin_pair}USDC"
            
            # Build TPSet
            tps = TPSet(
                tp1=ai_result.get("tp1"),
                tp2=ai_result.get("tp2"),
                tp3=ai_result.get("tp3")
            )
            
            # Create ParsedSignal (with correct field names)
            return ParsedSignal(
                raw_text=raw_text,  # Fixed: was rawtext
                currency_display=currency_display,  # Fixed: was currencydisplay
                symbol_hint=symbol_hint,  # Fixed: was symbolhint
                entry=float(ai_result.get("entry_price")),
                stop=float(ai_result.get("stop_loss")) if ai_result.get("stop_loss") else None,
                tps=tps,
                capital_pct=ai_result.get("capital_allocation"),  # Fixed: was capitalpct
                period_hours=ai_result.get("time_horizon_days") * 24 if ai_result.get("time_horizon_days") else None,  # Fixed: was periodhours
                spot_only=ai_result.get("spot_only", True)  # Fixed: was spotonly
            )
        except Exception as e:
            print(f"Conversion Error: {e}")
            return None
