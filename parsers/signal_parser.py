import re
from typing import Optional
from dataclasses import dataclass
from pydantic import BaseModel
from utils.events import emit

# For smart fallback
import json
import os

# Load aliases for fallback (LITECOIN -> LTC, Horizen -> ZEN, etc.)
ALIASES_PATH = os.path.join(os.path.dirname(__file__), "..", "token_aliases.json")
try:
    with open(ALIASES_PATH, "r", encoding="utf-8") as f:
        TOKEN_ALIASES = {k.upper(): v.upper() for k, v in json.load(f).items()}
except:
    TOKEN_ALIASES = {}


# ---------- Models ----------
class TPSet(BaseModel):
    tp1: float
    tp2: Optional[float] = None
    tp3: Optional[float] = None


@dataclass
class ParsedSignal:
    raw_text: str
    currency_display: str
    symbol_hint: Optional[str]
    entry: float
    stop: Optional[float]
    tps: TPSet
    capital_pct: Optional[float]
    period_hours: Optional[int]
    spot_only: bool = True


# ---------- Helpers ----------
def _m(text, pats):
    for p in pats:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return m.group(1) if m.lastindex else m.group(0)
    return None


def clean_num(val: str) -> float:
    return float(re.sub(r"[^\d.]", "", val.replace(",", "")))


def extract_symbol_hint(line: str):
    """
    Extracts a possible symbol hint written in parentheses, like (BTC/USDT) or (SOL),
    but safely ignores parentheses from TP/SL lines such as:
        (TP), (SL), (+1.08%), (TP1), etc.
    """
    line = line.strip()

    danger_words = ["TP", "TAKE PROFIT", "SL", "STOP LOSS", "SPOT"]
    if any(w in line.upper() for w in danger_words):
        return line, None

    p = re.search(r"\(([A-Z0-9]+)\)", line)
    if p:
        token = p.group(1).upper()
        ignored = {"TP", "SL", "SPOT", "TP1", "TP2", "TP3", "TP4"}
        if token not in ignored:
            return line, token

    s = re.search(r"([A-Z0-9]{2,})\s*/\s*[A-Z]{3,5}", line)
    if s:
        return line, s.group(1)

    return line, None


def days_or_hours_to_hours(text: str):
    t = text.lower()
    if m := re.search(r"(\d+)\s*(day|days)", t):
        return int(m.group(1)) * 24
    if m := re.search(r"(\d+)\s*(hour|hours)", t):
        return int(m.group(1))
    return None


# ---------- Regex Patterns ----------
CURRENCY_KEYS = [
    r"Currency\s*[:\-]\s*(.+)",
    r"Coin\s*[:\-]\s*(.+)",
    r"Asset\s*[:\-]\s*(.+)",
    r"الأصل\s*[:\-]\s*(.+)",
    r"العملة\s*[:\-]\s*(.+)",
    r"Währung\s*[:\-]\s*(.+)",
]

ENTRY_KEYS = [
    r"Entry(?: Price| Zone)?\s*[:\-]\s*\$?([\d\.,]+)",
]

STOP_KEYS = [
    r"Stop\s*Loss(?:\s*\(SL\))?\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?",
    r"وقف\s*الخسارة\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?",
    r"Stop-?Loss\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?",
]

TP_KEYS = [
    r"TP1\s*(?:[:\-—–→➔>])\s*\$?([\d\.,]+)",
    r"TP2\s*(?:[:\-—–→➔>])\s*\$?([\d\.,]+)",
    r"TP3\s*(?:[:\-—–→➔>])\s*\$?([\d\.,]+)",
    r"TP4\s*(?:[:\-—–→➔>])\s*\$?([\d\.,]+)",
    r"Take\s*Profit\s*\(?(TP\d*)?\)?\s*(?:[:\-—–→➔>])\s*\$?([\d\.,]+)",
    r"Target\s*\d*\s*(?:[:\-—–→➔>])\s*\$?([\d\.,]+)",
    r"الهدف\s*\d*\s*(?:[:\-—–→➔>])\s*\$?([\d\.,]+)",
    r"Ziel\s*\d*\s*(?:[:\-—–→➔>])\s*\$?([\d\.,]+)",
]

TP_KEYS += [
    r"Take\s*Profit\s*(?:1|2|3|4)\s*(?:[:\-—–→➔>])\s*\$?([\d\.,]+)",
    r"TP\s*(?:1|2|3|4)\s*(?:[:\-—–→➔>])\s*\$?([\d\.,]+)",
    r"Take\s*Profits?\s*[:\-—–]\s*\$?([\d\.,]+)",
    r"(?:^|\n)\s*[•\-\u25AA\u25CF\u25E6\u2022\u25AB\u25A0\u25C6\u25C7\u25B8\u25B9\u25B6\u25B7\u279C\u2794\u27A1\u27F6\u27F7\u2799\u279A\u279B\u27A4\u27B3\u27B2\u27BD\u27BE\u27A5\u27A6\u27A7\u27A8\u27A9\u27AB\u27AC\u27AD\u27AE\u27AF\u27B0\u27B1\u27BB\u27BC]?\s*\$?([\d\.,]+)",
]

CAPITAL_KEYS = [
    r"Capital(?: Entry| Allocation)?\s*[:\-]\s*\*?([\d\.]+)\*?\s*%",
    r"نسبة\s*(?:رأس\s*المال|الدخول)\s*[:\-]\s*\*?%?([\d\.]+)\*?",
    r"Kapitaleinsatz\s*[:\-]\s*\*?([\d\.]+)\*?\s*%",
]

PERIOD_KEYS = [
    r"Period\s*[:\-]\s*\*?([^\n\*]+)\*?",
    r"Duration\s*[:\-]\s*\*?([^\n\*]+)\*?",
    r"المدة\s*[:\-]\s*\*?([^\n\*]+)\*?",
    r"Zeitraum\s*[:\-]\s*\*?([^\n\*]+)\*?",
]

SPOT_ONLY_KEYS = [r"spot\s*only", r"spot", r"SPOT TRADE", r"فورية"]


# ---------- SMART FALLBACK RESOLVER ----------
def resolve_currency_fallback(text: str, cur: Optional[str]) -> Optional[str]:
    """
    Currently unused in parse_signal, but kept for compatibility.
    Uses strict word-boundary alias matching.
    """
    text_u = text.upper()

    if cur and cur.upper() != "SPOT":
        return cur

    for name, ticker in TOKEN_ALIASES.items():
        if len(ticker) <= 2:
            continue
        if re.search(rf"\b{name.upper()}\b", text_u):
            return ticker
        if re.search(rf"\b{ticker}\b", text_u):
            return ticker

    p = re.search(r"([A-Z]{2,10})\s*/\s*([A-Z]{2,10})", text_u)
    if p:
        return p.group(1)

    return cur


# ---------- Parser ----------
def parse_signal(text: str) -> Optional[ParsedSignal]:
    emit("parse_debug", {"stage": "start", "preview": text[:120]})

    # -------------------------------
    #  DEFINE CURRENCY ZONE (HEADER BEFORE ENTRY)
    # -------------------------------
    text_u = text.upper()

    # ONLY the English Entry marks the start of the valid zone
    # ENGLISH ONLY entry-related keywords
    entry_keywords = [
        r"\bENTRY\b",
        r"\bENTRY\s*PRICE\b",
        r"\bENTRY\s*ZONE\b",
        r"\bENTRY\s*RANGE\b",
        r"\bENTRY\s*TARGET\b",
        r"\bENTRY\s*LEVEL\b",
    ]

    # IMPORTANT: search inside uppercased text to find position
    entry_pos = None
    for kw in entry_keywords:
        m = re.search(kw, text_u, re.IGNORECASE)
        if m:
            entry_pos = m.start()
            break   # STOP at the first English match

    if entry_pos is not None:
        # ENGLISH ENTRY found → valid header
        currency_zone = text[:entry_pos]
    else:
        # IF ENGLISH ENTRY IS MISSING (rare), use only the FIRST 2 lines
        lines = text.split("\n")
        currency_zone = "\n".join(lines[:2]) if len(lines) >= 2 else text

    currency_zone_u = currency_zone.upper()

    emit("parse_debug", {
        "stage": "currency_zone",
        "entry_pos": entry_pos,
        "currency_zone_preview": currency_zone[:120],
    })

    # -------------------------------
    #  CURRENCY EXTRACTION (SAFE) - ONLY FROM CURRENCY ZONE
    # -------------------------------
    emit("parse_debug", {"stage": "currency_start", "preview": currency_zone[:120]})

    cur = None

    # 1️⃣ explicit trading pairs - SEARCH ONLY IN CURRENCY ZONE
    pair_match = re.search(r"\b([A-Z0-9]{2,10})\s*/\s*([A-Z0-9]{2,10})\b", currency_zone_u)
    if pair_match:
        cur = pair_match.group(1)
        emit("parse_debug", {"stage": "currency_pair_detected", "currency": cur})
    else:
        # 2️⃣ "Coin:" or "Currency:" or "Asset:" fields - SEARCH ONLY IN CURRENCY ZONE
        coin_field = _m(currency_zone, CURRENCY_KEYS)
        if coin_field:
            coin_field = coin_field.strip().upper()
            
            # Clean up: remove (SPOT), (FUTURES), etc. from the field
            coin_field = re.sub(r'\s*\((?:SPOT|FUTURES|PERP|PERPETUAL)\)\s*', '', coin_field)
            coin_field = coin_field.strip()

            for name, ticker in TOKEN_ALIASES.items():
                if len(ticker) <= 2:
                    continue
                if coin_field == name.upper():
                    cur = ticker
                    break
                if coin_field == ticker:
                    cur = ticker
                    break

            if not cur:
                cur = coin_field

        # 3️⃣ Parentheses extraction - HIGHEST PRIORITY - SEARCH ONLY IN CURRENCY ZONE
        if not cur:
            # Look for ticker in parentheses like (ETC) or (BTC)
            parens = re.findall(r"\(([A-Z0-9]{2,15})\)", currency_zone_u)
            for token in parens:
                token = token.upper()
                if token in {"TP", "SL", "TP1", "TP2", "TP3", "TP4", "SPOT"}:
                    continue
                if re.fullmatch(r"\d+(\.\d+)?", token):
                    continue

                # If it's a valid ticker (2-10 chars), use it directly
                if 2 <= len(token) <= 10:
                    # Check if it's in our known tickers
                    if token in TOKEN_ALIASES.values():
                        cur = token
                        break
                    # Check if it's an alias
                    if token in TOKEN_ALIASES:
                        cur = TOKEN_ALIASES[token]
                        break
                    # If it looks like a ticker, accept it (parentheses are strong signal)
                    cur = token
                    break

        # 4️⃣ SAFE alias scan — **LIMITED TO CURRENCY ZONE**
        if not cur:
            # First, look for exact ticker matches (like "ETC" in the text)
            for name, ticker in TOKEN_ALIASES.items():
                # Skip tiny tickers (S, H, PE…)
                if len(ticker) <= 2:
                    continue
                    
                # PRIORITY: Look for the ticker itself (e.g., "ETC")
                if re.search(rf"\b{ticker}\b", currency_zone_u):
                    cur = ticker
                    break
            
            # Second, look for full coin names (but only if no ticker found)
            if not cur:
                for name, ticker in TOKEN_ALIASES.items():
                    # Skip tiny tickers (S, H, PE…)
                    if len(ticker) <= 2:
                        continue
                    
                    # Look for full coin name like "LITECOIN" or "BITCOIN"
                    # But NOT partial matches like "ETHEREUM" in "ETHEREUM CLASSIC"
                    if re.search(rf"\b{name.upper()}\b", currency_zone_u):
                        # Double-check: if we found "ETHEREUM", make sure "CLASSIC" isn't right after
                        match_pos = currency_zone_u.find(name.upper())
                        if match_pos != -1:
                            after_match = currency_zone_u[match_pos + len(name):match_pos + len(name) + 20]
                            # If we see "CLASSIC", "CASH", "GOLD" after the coin name, skip it
                            if not re.match(r'\s*(CLASSIC|CASH|GOLD|SV|ABC)', after_match):
                                cur = ticker
                                break

        # 5️⃣ Look for token after "Signal —" or similar patterns in FIRST LINE ONLY
        if not cur:
            # Get the actual first line (before first newline)
            first_line = currency_zone.split("\n")[0].strip()
            first_line_u = first_line.upper()
            
            # Pattern: "Signal — TOKEN" or "Signal - TOKEN"
            m = re.search(r"SIGNAL\s*[—\-–]\s*([A-Z0-9]{2,10})", first_line_u)
            if m:
                token = m.group(1).upper()
                # Verify it's not a common word
                if token not in {"HIGH", "MEDIUM", "LOW", "RISK", "SPOT", "THE", "FOR", "AND", "LEVEL"}:
                    cur = token
                    emit("parse_debug", {"stage": "currency_from_signal_dash", "currency": cur})

    emit("parse_debug", {"stage": "currency_extracted", "currency": cur})

    # ------------------ ENTRY - SEARCH IN FULL TEXT ------------------
    entry_match = None
    for pat in ENTRY_KEYS:
        entry_match = re.search(pat, text, re.IGNORECASE)
        if entry_match:
            break

    entry = None
    if entry_match:
        if entry_match.lastindex and entry_match.lastindex >= 2 and entry_match.group(2):
            entry = str((clean_num(entry_match.group(1)) + clean_num(entry_match.group(2))) / 2)
        else:
            entry = entry_match.group(1)

    # Stop Loss - SEARCH IN FULL TEXT
    stop = _m(text, STOP_KEYS)

    # Take Profits - SEARCH IN FULL TEXT
    tp_values = []
    for pat in TP_KEYS:
        for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
            try:
                val = m.group(m.lastindex or 1)
            except:
                continue
            if val and re.search(r"\d", val):
                tp_values.append(val.strip())

    seen = set()
    tp_values = [x for x in tp_values if not (x in seen or seen.add(x))]

    # Capital & Period - SEARCH IN FULL TEXT
    cap = _m(text, CAPITAL_KEYS)
    per = _m(text, PERIOD_KEYS)
    spot_only = any(re.search(k, text, re.IGNORECASE) for k in SPOT_ONLY_KEYS)

    emit("parse_debug", {
        "stage": "fields",
        "currency": cur,
        "entry": entry,
        "stop": stop,
        "tp1": tp_values[0] if tp_values else None,
        "tp2": tp_values[1] if len(tp_values) > 1 else None,
        "tp3": tp_values[2] if len(tp_values) > 2 else None,
        "cap": cap,
        "per": per,
    })

    if not (cur and entry):
        return None

    if len(tp_values) == 0:
        try:
            e = clean_num(entry)
            tp_values = [str(round(e * 1.03, 8))]
        except:
            return None

    currency_display, symbol_hint = extract_symbol_hint(cur)
    if not symbol_hint and "/" in cur:
        symbol_hint = cur.split("/")[0].strip().upper()

    stop_val = clean_num(stop) if stop else None

    tp1 = clean_num(tp_values[0]) if len(tp_values) >= 1 else None
    tp2 = clean_num(tp_values[1]) if len(tp_values) >= 2 else None
    tp3 = clean_num(tp_values[2]) if len(tp_values) >= 3 else None

    def _norm(v):
        try:
            return round(float(v), 6)
        except:
            return None

    stop_val = _norm(stop_val)
    tp1 = _norm(tp1)
    tp2 = _norm(tp2)
    tp3 = _norm(tp3)

    if tp1 is None:
        return None

    tpset = TPSet(tp1=tp1, tp2=tp2, tp3=tp3)

    return ParsedSignal(
        raw_text=text,
        currency_display=currency_display,
        symbol_hint=symbol_hint,
        entry=clean_num(entry),
        stop=stop_val,
        tps=tpset,
        capital_pct=float(cap) / 100.0 if cap else None,
        period_hours=days_or_hours_to_hours(per) if per else None,
        spot_only=spot_only,
    )