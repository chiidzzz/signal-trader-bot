# parsers/signal_parser.py
import re
from typing import Optional
from dataclasses import dataclass
from pydantic import BaseModel
from utils.events import emit

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
    # Normalize things like "$4,100.50", " 4.10 ", "4.10%" etc.
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
    # Handles single price or range, with optional $ and bold/escaped $ and fancy dashes
    r"Entry(?: Price| Zone)?\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?(?:\s*[–\-—]\s*\*?\\?\$?([\d\.,]+)\*?)?",
    r"سعر\s*الدخول\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?(?:\s*[–\-—]\s*\*?\\?\$?([\d\.,]+)\*?)?",
    r"Einstieg(?:szone)?\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?(?:\s*[–\-—]\s*\*?\\?\$?([\d\.,]+)\*?)?",
]

STOP_KEYS = [
    r"Stop\s*Loss(?:\s*\(SL\))?\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?",
    r"وقف\s*الخسارة\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?",
    r"Stop-?Loss\s*[:\-]\s*\*?\\?\$?([\d\.,]+)\*?",
]

# --- TP patterns ---
# Keep your existing list exactly as requested, then we append more catch-alls.
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

# Additional flexible patterns we append (do NOT change the ones above)
# These catch variants like "Take Profit 1: 3.33", "TP 2 -> 3.44", etc.
TP_KEYS += [
    r"Take\s*Profit\s*(?:1|2|3|4)\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"TP\s*(?:1|2|3|4)\s*(?:[:\-–—→➝>])\s*\$?([\d\.,]+)",
    r"Take\s*Profits?\s*[:\-–—]\s*\$?([\d\.,]+)",  # sometimes a single "Take Profits: 4.10"
    # Fallback for bullet-style under a TP section: lines that are just numbers with arrows
    r"(?:^|\n)\s*[•\-\u25AA\u25CF\u25E6\u2022\u25AB\u25A0\u25C6\u25C7\u25B8\u25B9\u25B6\u25B7\u279C\u2794\u27A1\u27F6\u27F7\u2799\u279A\u279B\u27A4\u27B3\u27B2\u27BD\u27BE\u27A5\u27A6\u27A7\u27A8\u27A9\u27AB\u27AC\u27AD\u27AE\u27AF\u27B0\u27B1\u27BB\u27BC]?\s*\$?([\d\.,]+)\s*(?:[+→➝>\-–—]\s*[\d\.\,%]+)?\s*(?:$|\n)",
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

# ---------- Parser ----------
def parse_signal(text: str) -> Optional[ParsedSignal]:
    emit("parse_debug", {"stage": "start", "preview": text[:120]})

    # Currency
    cur = _m(text, CURRENCY_KEYS)
    if not cur:
        # Try formats like: "🔥 XRP (Spot Trade) 🔥"
        cur_line = re.search(r"([A-Z]{2,10})\s*\(?(?:Spot|Trade|Spot Trade)?\)?", text)
        if cur_line:
            cur = cur_line.group(1)
    # Try to detect things like "Solana / Spot Trading"
    if not cur:
        cur_line = re.search(r"([A-Za-z]{2,15})\s*/\s*(?:Spot|Trading|Trade|Spot Trading)", text, re.IGNORECASE)
        if cur_line:
            cur = cur_line.group(1)
    if not cur:
        cur_line = re.search(r"([A-Za-z]{2,15})\s+(?:Spot|Signal|Trade)", text, re.IGNORECASE)
        if cur_line:
            cur = cur_line.group(1)

    if not cur:
        # As final fallback: "XRP / USDT"
        cur_line = re.search(r"[A-Z]{2,10}\s*/\s*[A-Z]{2,5}", text)
        if cur_line:
            cur = cur_line.group(0)

    # Entry (supports single or range)
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

    # Stop Loss
    stop = _m(text, STOP_KEYS)

    # Take Profits
    tp_values = []
    for pat in TP_KEYS:
        for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
            try:
                val = m.group(m.lastindex or 1)
            except IndexError:
                continue
            if val:
                val_norm = val.strip()
                # Avoid collecting obvious % only or too-short junk
                if re.search(r"\d", val_norm):
                    tp_values.append(val_norm)

    # Deduplicate while preserving order
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
        "tp1": tp_values[0] if len(tp_values) > 0 else None,
        "tp2": tp_values[1] if len(tp_values) > 1 else None,
        "tp3": tp_values[2] if len(tp_values) > 2 else None,
        "cap": cap,
        "per": per,
    })

    # Sanity check: must have currency + entry
    if not (cur and entry):
        return None

    # If NO TP found, set TP1 to +3% above entry and notify
    if len(tp_values) == 0:
        try:
            e_val = clean_num(entry)
            fallback_tp = round(e_val * 1.03, 8)
            emit("parse_notice", {
                "msg": "No TP detected — applying fallback TP1 = entry * 1.03",
                "entry": e_val,
                "tp1": fallback_tp
            })
            tp_values = [str(fallback_tp)]
        except Exception:
            # if entry couldn’t be parsed to float for some reason — fail silently to keep old behavior
            pass

    # Resolve currency display & hint
    currency_display, symbol_hint = extract_symbol_hint(cur)
    if not symbol_hint and "/" in cur:
        symbol_hint = cur.split("/")[0].strip().upper()

    stop_val = clean_num(stop) if stop else None
    # Build TP set
    tp1 = clean_num(tp_values[0]) if len(tp_values) >= 1 else None
    tp2 = clean_num(tp_values[1]) if len(tp_values) >= 2 else None
    tp3 = clean_num(tp_values[2]) if len(tp_values) >= 3 else None
    
    # --- Normalize numeric precision to Binance-safe float ---
    def _normalize_price(v):
        try:
            return round(float(v), 6) if v is not None else None
        except Exception:
            return None

    stop_val = _normalize_price(stop_val)
    tp1 = _normalize_price(tp1)
    tp2 = _normalize_price(tp2)
    tp3 = _normalize_price(tp3)


    # If still no TP1 for some reason, abort
    if tp1 is None:
        return None

    tpset = TPSet(tp1=tp1, tp2=tp2, tp3=tp3)

    emit("parse_success", {
        "currency": cur,
        "entry": clean_num(entry),
        "sl": stop_val,
        "tp1": tpset.tp1,
        "tp2": tpset.tp2,
        "tp3": tpset.tp3,
    })

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
