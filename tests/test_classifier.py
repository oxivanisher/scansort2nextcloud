import random

import pytest

from scansort.classifier import ModelStore, NotEnoughData, Sample, propose, train
from scansort.config import Rule

CORPUS = {
    "Versicherung/Krankenkasse": ["Krankenkasse Prämie Grundversicherung Franchise Leistungsabrechnung Arzt",
                                  "Krankenversicherung Prämienrechnung Selbstbehalt Spital Leistungen"],
    "Steuern/{year}": ["Steuererklärung Steuerverwaltung Einkommen Vermögen Veranlagung Steuerbetrag",
                       "Provisorische Steuerrechnung Kantonssteuer Gemeindesteuer Bundessteuer"],
    "Telefon/Swisscom": ["Swisscom Rechnung Mobile Abo Internet TV Festnetz Datenvolumen",
                         "Swisscom Mobile Rechnung Roaming Gesprächsminuten Abonnement"],
}


def samples(n=4):
    rng = random.Random(1)
    out = []
    for label, texts in CORPUS.items():
        for i in range(n):
            words = texts[i % len(texts)].split()
            rng.shuffle(words)
            out.append(Sample(" ".join(words) + f" Dokument {i}", label))
    return out


def test_train_and_predict(tmp_path):
    pipeline, info = train(samples() + [Sample("einzelnes Dokument", "Rare/Label")], min_samples=2)
    assert info.labels == 3
    assert info.skipped_labels == ["Rare/Label"]
    assert info.cv_accuracy is not None and info.cv_accuracy > 0.6

    store = ModelStore(tmp_path / "model.joblib")
    store.replace(pipeline, info)
    preds = store.predict("Ihre Swisscom Mobile Rechnung für das Abo", top_n=3)
    assert preds[0].label == "Telefon/Swisscom"
    assert len(preds) == 3 and preds[0].probability >= preds[1].probability

    reloaded = ModelStore(tmp_path / "model.joblib")
    assert reloaded.load()
    assert reloaded.predict("Steuererklärung Einkommen")[0].label == "Steuern/{year}"


def test_not_enough_data():
    with pytest.raises(NotEnoughData):
        train([Sample("a b c", "A"), Sample("a b d", "A")], min_samples=2)
    with pytest.raises(NotEnoughData):
        train([Sample("a", "A"), Sample("b", "B")], min_samples=2)


def test_rules_first(tmp_path):
    store = ModelStore(tmp_path / "m.joblib")
    store.replace(*train(samples()))
    rules = [Rule(pattern="(?i)steuererkl(ä|ae)rung", label="Steuern/{year}")]
    preds = propose("Steuererklaerung 2025 Swisscom", None, store=store, rules=rules, top_n=3)
    assert preds[0].source == "rule" and preds[0].label == "Steuern/{year}"
    assert [p.label for p in preds].count("Steuern/{year}") == 1
    assert len(preds) == 3


def test_cold_start(tmp_path):
    store = ModelStore(tmp_path / "none.joblib")
    assert not store.load()
    assert propose("anything", None, store=store, rules=[], top_n=3) == []
