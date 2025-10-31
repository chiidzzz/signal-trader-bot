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
    r"Entry(?: Price| Zone)?\s*[:\-]\s*\$?([\d\.,]+)(?:\s*[–\-—→➝>]\s*\$?([\d\.,]+))?",
    r"سعر\s*الدخول\s*[:\-]\s*\$?([\d\.,]+)(?:\s*[–\-—→➝>]\s*\$?([\d\.,]+))?",
    r"Einstieg(?:spreis|szone)?\s*[:\-]\s*\$?([\d\.,]+)(?:\s*[–\-—→➝>]\s*\$?([\d\.,]+))?",
]

STOP_KEYS = [
    r"Stop\s*Loss(?:\s*\(SL\))?\s*[:\-→➝>]\s*\$?([\d\.,]+)",
    r"وقف\s*الخسارة\s*[:\-→➝>]\s*\$?([\d\.,]+)",
    r"Stop-?Loss\s*[:\-→➝>]\s*\$?([\d\.,]+)",
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

CAPITAL_KEYS = [
    r"Capital(?: Entry| Allocation)?\s*[:\-]\s*([\d\.]+)\s*%",
    r"نسبة\s*(?:رأس\s*المال|الدخول)\s*[:\-]\s*%?([\d\.]+)",
    r"Kapitaleinsatz\s*[:\-]\s*([\d\.]+)\s*%",
]

PERIOD_KEYS = [
    r"Period\s*[:\-]\s*([^\n\*]+)",
    r"Duration\s*[:\-]\s*([^\n\*]+)",
    r"المدة\s*[:\-]\s*([^\n\*]+)",
    r"Zeitraum\s*[:\-]\s*([^\n\*]+)",
]

SPOT_ONLY_KEYS = [r"spot\s*only", r"spot", r"SPOT TRADE", r"فورية"]


# ---------- Parser ----------
def parse_signal(text: str) -> Optional[ParsedSignal]:
    emit("parse_debug", {"stage": "start", "preview": text[:120]})

    cur = _m(text, CURRENCY_KEYS)
    if not cur:
        cur_line = re.search(r"([A-Z]{2,10})\s*\(?(?:Spot|Trade|Spot Trade)?\)?", text)
        if cur_line:
            cur = cur_line.group(1)
    if not cur:
        cur_line = re.search(r"[A-Z]{2,10}\s*/\s*[A-Z]{2,5}", text)
        if cur_line:
            cur = cur_line.group(0)

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

    stop = _m(text, STOP_KEYS)

    # --- TP extraction ---
    tp_values = []
    # Normal explicit TP patterns
    for pat in TP_KEYS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            val = m.group(m.lastindex or 1)
            if val and val not in tp_values:
                tp_values.append(val)

    # Fallback: numeric bullet list after "Targets" or "الأهداف"
    if not tp_values:
        after_targets = re.split(r"(?:Targets|Take\s*Profits|الأهداف|Kursziele)[:：]?", text, flags=re.IGNORECASE)
        if len(after_targets) > 1:
            section = after_targets[1]
            nums = re.findall(r"[\d]+\.\d+", section)
            for n in nums[:3]:
                if n not in tp_values:
                    tp_values.append(n)

    tp1 = tp_values[0] if len(tp_values) >= 1 else None
    tp2 = tp_values[1] if len(tp_values) >= 2 else None
    tp3 = tp_values[2] if len(tp_values) >= 3 else None

    cap = _m(text, CAPITAL_KEYS)
    per = _m(text, PERIOD_KEYS)
    spot_only = any(re.search(k, text, re.IGNORECASE) for k in SPOT_ONLY_KEYS)

    emit("parse_debug", {
        "stage": "fields",
        "currency": cur,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "cap": cap,
        "per": per,
    })

    if not (cur and entry and tp1):
        return None

    currency_display, symbol_hint = extract_symbol_hint(cur)
    if not symbol_hint and "/" in cur:
        symbol_hint = cur.split("/")[0].strip().upper()

    stop_val = clean_num(stop) if stop else None
    tpset = TPSet(
        tp1=clean_num(tp1),
        tp2=clean_num(tp2) if tp2 else None,
        tp3=clean_num(tp3) if tp3 else None,
    )

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
