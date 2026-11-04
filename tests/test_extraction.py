from datetime import date

import pytest

from scansort.config import ExtractionSettings, KnownSender
from scansort.extraction import extract_amount, extract_date, extract_fields, extract_sender

CFG = ExtractionSettings(known_senders=[KnownSender(name="CSS", patterns=["css versicherung", r"css\s+kranken"])])
TODAY = date(2026, 6, 1)


@pytest.mark.parametrize("text,expected", [
    ("Zürich, 03.02.2025", date(2025, 2, 3)),
    ("Datum: 3.2.25", date(2025, 2, 3)),
    ("2024-11-30 statement", date(2024, 11, 30)),
    ("Bern, den 4. März 2026", date(2026, 3, 4)),
    ("Basel, 12. Januar 2024", date(2024, 1, 12)),
    ("London, 5 November 2025", date(2025, 11, 5)),
    ("Date: November 5, 2025", date(2025, 11, 5)),
])
def test_date_formats(text, expected):
    assert extract_date(text, CFG, TODAY) == expected


def test_prefers_keyword_date():
    text = "Kundennummer 01.01.2020 seit\nVertrag vom 15.03.2019\nRechnungsdatum: 20.05.2026\nFällig 19.06.2026"
    assert extract_date(text, CFG, TODAY) == date(2026, 5, 20)


def test_first_date_without_keywords():
    assert extract_date("blah 10.04.2026 foo 11.04.2026", CFG, TODAY) == date(2026, 4, 10)


def test_rejects_future_and_too_old():
    assert extract_date("valid until 31.12.2030", CFG, TODAY) is None
    assert extract_date("born 01.01.1950", CFG, TODAY) is None


def test_rejects_invalid_and_versions():
    assert extract_date("31.02.2025 and version 1.2.3", CFG, TODAY) is None


def test_fields_fallback_guessed():
    f = extract_fields("no date here", CFG, TODAY)
    assert f.doc_date == TODAY and f.doc_date_guessed


def test_sender():
    assert extract_sender("Ihre CSS Krankenversicherung AG", CFG) == "CSS"
    assert extract_sender("Ihre CSS   Kranken-Versicherung", CFG) == "CSS"
    assert extract_sender("Nothing", CFG) is None


@pytest.mark.parametrize("text,expected", [
    ("Total CHF 1'234.50", "CHF 1234.50"),
    ("Gesamtbetrag 1.234,50 EUR", "EUR 1234.50"),
    ("Zu bezahlen: Fr. 99.--", "CHF 99.00"),
    ("Betrag € 12,00\nMwSt CHF 3.00", "EUR 12.00"),
    ("CHF 50.00 ohne Stichwort", None),
])
def test_amount(text, expected):
    assert extract_amount(text, CFG) == expected
