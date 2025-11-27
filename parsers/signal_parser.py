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
    line = line.strip()
    p = re.search(r"\(([A-Z0-9]+)\)", line)
    if p:
        return line, p.group(1)
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
    r"العملة\s*[:\-]\s*(.+)",
    r"Währung\s*[:\-]\s*(.+)",
]

ENTRY_KEYS = [
    r"Entry(?: Price| Zone)?\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?(?:\s*[–\-—]\s*\*?\\?\$?([\d\.,]+)\*?)?",
    r"سعر\s*الدخول\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?(?:\s*[–\-—]\s*\*?\\?\$?([\d\.,]+)\*?)?",
    r"Einstieg(?:szone)?\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?(?:\s*[–\-—]\s*\*?\\?\$?([\d\.,]+)\*?)?",
]

STOP_KEYS = [
    r"Stop\s*Loss(?:\s*\(SL\))?\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?",
    r"وقف\s*الخسارة\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?",
    r"Stop-?Loss\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?",
]

TP_KEYS = [
    r"TP1\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"TP2\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"TP3\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"TP4\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"Take\s*Profit\s*\(?(TP\d*)?\)?\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"Target\s*\d*\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"الهدف\s*\d*\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"Ziel\s*\d*\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
]

TP_KEYS += [
    r"Take\s*Profit\s*(?:1|2|3|4)\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"TP\s*(?:1|2|3|4)\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"Take\s*Profits?\s*[:\-–—]\s*\$?([\d\.,]+)",
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
    text_u = text.upper()

    # If parser already found a valid ticker, keep it
    if cur and cur.upper() != "SPOT":
        return cur

    # 1️⃣ Search for exact tickers in aliases
    for name, ticker in TOKEN_ALIASES.items():
        if ticker in text_u:
            return ticker

    # 2️⃣ Search for coin names in aliases
    for name, ticker in TOKEN_ALIASES.items():
        if name in text_u:
            return ticker

    # 3️⃣ Search for explicit trading pairs
    p = re.search(r"([A-Z]{2,10})\s*/\s*([A-Z]{2,10})", text_u)
    if p:
        return p.group(1)

    return cur


# ---------- Parser ----------
def parse_signal(text: str) -> Optional[ParsedSignal]:
    emit("parse_debug", {"stage": "start", "preview": text[:120]})

    # -------------------------------
    #  CURRENCY EXTRACTION (SAFEST VERSION)
    # -------------------------------
    emit("parse_debug", {"stage": "currency_start", "preview": text[:120]})

    cur = None

    # 1️⃣ Highest-priority: (TICKER)
    paren = re.search(r"\(([A-Z0-9]{2,10})\)", text)
    if paren:
        cur = paren.group(1)

    # 2️⃣ Coin name before Spot/Signal
    if not cur:
        name_match = re.search(
            r"([A-Za-z]{3,20})\s*[—\-–]*\s*(?:Spot|Signal)",
            text,
            flags=re.IGNORECASE
        )
        if name_match:
            cur = name_match.group(1)

    # 3️⃣ "Currency:", "Coin:", etc.
    if not cur:
        cur = _m(text, CURRENCY_KEYS)

    # 4️⃣ "Solana Spot"
    if not cur:
        m = re.search(r"([A-Za-z]{2,20})\s+(?:Spot|Signal|Trade)", text, re.IGNORECASE)
        if m:
            cur = m.group(1)

    # 5️⃣ "Solana / Spot Trading"
    if not cur:
        m = re.search(r"([A-Za-z]{2,15})\s*/\s*(?:Spot|Trading|Trade|Spot Trading)", text, re.IGNORECASE)
        if m:
            cur = m.group(1)

    # 6️⃣ Explicit pair
    if not cur:
        m = re.search(r"([A-Z]{2,10}\s*/\s*[A-Z]{2,5})", text)
        if m:
            cur = m.group(1)

    # Prevent SPOT being mistaken as currency
    if cur and cur.upper() == "SPOT":
        cur = None

    # 7️⃣ SMART FALLBACK
    cur = resolve_currency_fallback(text, cur)

    emit("parse_debug", {"stage": "currency_extracted", "currency": cur})

    # --- Entry ---
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

    # --- Stop Loss ---
    stop = _m(text, STOP_KEYS)

    # --- Take Profits ---
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

    # Capital & Period
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

    # If no currency or entry → fail
    if not (cur and entry):
        return None

    # Fallback TP1 if none found
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
        try: return round(float(v), 6)
        except: return None

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
