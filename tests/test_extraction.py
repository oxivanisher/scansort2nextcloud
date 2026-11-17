from datetime import date

import pytest

from scansort.config import ExtractionSettings, KnownSender
from scansort.extraction import (
    date_choices,
    document_types,
    extract_amount,
    extract_date,
    extract_fields,
    extract_sender,
    extract_type,
)

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


def test_date_choices_ranked_and_distinct():
    text = "Zürich, 15.03.2026\nRechnungsdatum: 12.03.2026\nFällig am 12.04.2026\nRef 15.03.2026\nfuture 01.01.2030"
    choices = date_choices(text, CFG, TODAY)
    assert [c.value for c in choices] == [date(2026, 3, 12), date(2026, 3, 15), date(2026, 4, 12)]
    assert choices[0].keyword == "Rechnungsdatum" and choices[1].keyword is None
    assert choices[0].value == extract_date(text, CFG, TODAY)
    assert len(date_choices(" ".join(f"{d}.01.2026" for d in range(1, 20)), CFG, TODAY, limit=4)) == 4
    assert date_choices("", CFG, TODAY) == []


@pytest.mark.parametrize("text,expected", [
    ("Swisscom AG\nRechnung Nr. 4711\nRechnungsdatum 05.01.2025", "Rechnung"),
    ("Verwaltung AG\nNEBENKOSTENABRECHNUNG 2025\nAbrechnung der Heizkosten", "Nebenkostenabrechnung"),
    ("Sehr geehrte Damen und Herren, zu Ihrer Rechnung vom 1. März\nMahnung\n", "Mahnung"),  # heading wins
    ("ACME Ltd\nINVOICE #123\nBank statement attached", "Invoice"),
    ("Acme Bank\nBank statement\nperiod 01.01.-31.01.", "Bank statement"),
    ("Rechnungsnummer 4711, Rechnungsdatum 05.01.2025", None),  # whole words only
    ("Berechnung der Prämie", None),
    ("", None),
])
def test_extract_type(text, expected):
    assert extract_type(text, CFG) == expected


def test_document_types_ranked_and_limited():
    text = "Firma AG\nGutschrift\nDiese Gutschrift bezieht sich auf die Rechnung 4711 und den Vertrag 12."
    assert [c.name for c in document_types(text, CFG)] == ["Gutschrift", "Rechnung", "Vertrag"]
    assert [c.name for c in document_types(text, CFG, limit=2)] == ["Gutschrift", "Rechnung"]
    assert document_types("x" * 3000 + " Rechnung", CFG) == []  # only the top of the document
    custom = ExtractionSettings(document_types=["Stromrechnung"])
    assert extract_type("EWK\nStromrechnung 2025\nRechnung", custom) == "Stromrechnung"
    assert extract_fields("Rechnung\nDatum 05.01.2025", CFG, TODAY).doc_type == "Rechnung"
