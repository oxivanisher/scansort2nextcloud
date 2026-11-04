"""Field extraction from document text: document date, sender, amount."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from scansort.config import ExtractionSettings

_NUMERIC_DMY = re.compile(r"(?<![\d.])(\d{1,2})\s?[./]\s?(\d{1,2})\s?[./]\s?(\d{4}|\d{2})(?![\d.]\d)")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_WORD = r"[A-Za-zÄÖÜäöüéèûôç]{3,10}\.?"
_DAY_MONTH_YEAR = re.compile(rf"(?<!\d)(\d{{1,2}})\.?\s+({_WORD})\s+(\d{{4}})(?!\d)")
_MONTH_DAY_YEAR = re.compile(rf"\b({_WORD})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)")

_KEYWORD_WINDOW = 40

_AMOUNT_NUM = r"\d{1,3}(?:['’ .,]\d{3})*(?:[.,](?:\d{2}|-{1,2}))|\d+(?:[.,](?:\d{2}|-{1,2}))?"
_CUR = r"CHF|EUR|USD|SFr\.?|Fr\.|€|\$"
_AMOUNT = re.compile(
    rf"(?P<cur1>{_CUR})\s*(?P<num1>{_AMOUNT_NUM})|(?P<num2>{_AMOUNT_NUM})\s*(?P<cur2>{_CUR})(?!\w)"
)
_CUR_NORMAL = {"SFR": "CHF", "SFR.": "CHF", "FR.": "CHF", "€": "EUR", "$": "USD"}


@dataclass
class DateCandidate:
    pos: int
    value: date
    score: int


@dataclass
class ExtractedFields:
    doc_date: date | None
    doc_date_guessed: bool
    sender: str | None
    amount: str | None


@lru_cache(maxsize=1)
def _dateparser():
    import dateparser  # slow import; load lazily

    return dateparser


def _two_digit_year(y: int) -> int:
    return 2000 + y if y < 70 else 1900 + y


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _parse_words(text: str, languages: list[str]) -> date | None:
    try:
        dt = _dateparser().parse(
            text,
            languages=languages or None,
            settings={"STRICT_PARSING": True, "DATE_ORDER": "DMY", "RETURN_AS_TIMEZONE_AWARE": False},
        )
    except Exception:  # noqa: BLE001 - dateparser can raise on odd input
        return None
    return dt.date() if dt else None


def find_dates(text: str, cfg: ExtractionSettings, today: date | None = None) -> list[DateCandidate]:
    today = today or date.today()
    raw: list[tuple[int, date]] = []
    for m in _NUMERIC_DMY.finditer(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if len(m.group(3)) == 2:
            y = _two_digit_year(y)
        if (v := _safe_date(y, mo, d)) is not None:
            raw.append((m.start(), v))
    for m in _ISO.finditer(text):
        if (v := _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))) is not None:
            raw.append((m.start(), v))
    for rx in (_DAY_MONTH_YEAR, _MONTH_DAY_YEAR):
        for m in rx.finditer(text):
            if (v := _parse_words(m.group(0), cfg.date_languages)) is not None:
                raw.append((m.start(), v))

    keywords = [k.lower() for k in cfg.date_keywords]
    lowered = text.lower()
    out: list[DateCandidate] = []
    seen: set[int] = set()
    for pos, value in sorted(raw):
        if pos in seen or value > today or value.year < cfg.min_year:
            continue
        seen.add(pos)
        window = lowered[max(0, pos - _KEYWORD_WINDOW):pos]
        score = 0
        for kw in keywords:
            idx = window.rfind(kw)
            if idx >= 0 and re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", window):
                # closer keywords and longer (more specific) keywords score higher
                score = max(score, 10 + len(kw) - (len(window) - idx) // 10)
        out.append(DateCandidate(pos, value, score))
    return out


def extract_date(text: str, cfg: ExtractionSettings, today: date | None = None) -> date | None:
    candidates = find_dates(text, cfg, today)
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (c.score, -c.pos))
    return best.value


def extract_sender(text: str, cfg: ExtractionSettings) -> str | None:
    for sender in cfg.known_senders:
        patterns = sender.patterns or [re.escape(sender.name)]
        for pat in patterns:
            try:
                if re.search(pat, text, re.IGNORECASE):
                    return sender.name
            except re.error:
                if pat.lower() in text.lower():
                    return sender.name
    return None


def _parse_amount(num: str) -> Decimal | None:
    num = num.replace("’", "").replace("'", "").replace(" ", "")
    num = re.sub(r"[.,]-{1,2}$", "", num)  # "120.--"
    if "," in num and "." in num:
        dec = "," if num.rfind(",") > num.rfind(".") else "."
        thou = "." if dec == "," else ","
        num = num.replace(thou, "").replace(dec, ".")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        num = head.replace(",", "") + "." + tail if len(tail) == 2 else num.replace(",", "")
    elif num.count(".") > 1 or (num.count(".") == 1 and len(num.rpartition(".")[2]) == 3):
        num = num.replace(".", "")
    try:
        return Decimal(num)
    except InvalidOperation:
        return None


def extract_amount(text: str, cfg: ExtractionSettings) -> str | None:
    keywords = [k.lower() for k in cfg.amount_keywords]
    best: tuple[Decimal, str] | None = None
    for line in text.splitlines():
        low = line.lower()
        if not any(k in low for k in keywords):
            continue
        for m in _AMOUNT.finditer(line):
            cur = (m.group("cur1") or m.group("cur2") or "").upper()
            cur = _CUR_NORMAL.get(cur, cur)
            value = _parse_amount(m.group("num1") or m.group("num2") or "")
            if value is None or value <= 0:
                continue
            if best is None or value > best[0]:
                best = (value, cur)
    if best is None:
        return None
    return f"{best[1]} {best[0]:.2f}"


def extract_fields(text: str, cfg: ExtractionSettings, today: date | None = None) -> ExtractedFields:
    today = today or date.today()
    doc_date = extract_date(text, cfg, today) if text else None
    return ExtractedFields(
        doc_date=doc_date or today,
        doc_date_guessed=doc_date is None,
        sender=extract_sender(text, cfg) if text else None,
        amount=extract_amount(text, cfg) if text else None,
    )
