from scansort.classifier import Prediction
from scansort.companies import CompanyIndex, apply_companies, core_name, same_company
from scansort.labels import LabelRules

LABELS = {
    "_Companies/Salt": 3,
    "_Companies/EWK Hezogenbuchsee AG/PV Anlage": 3,
    "_Companies/EWK Hezogenbuchsee AG/Stromrechnungen/{year}": 16,
    "_Companies/Post": 1,
    "_Companies/Post/Zolldokumente": 2,
    "_Finanzinstitute/PostFinance/Visa/{year}": 3,
    "_Versicherungen/Mobiliar/{year}": 7,
    "_Steuern/Steuerrechnungen/{year}": 8,
}
FOLDERS = ["_Companies", "_Versicherungen", "_Finanzinstitute"]


def index():
    return CompanyIndex(LABELS, FOLDERS)


def model(*pairs):
    return [Prediction(lbl, p, "model") for lbl, p in pairs]


def test_names():
    assert core_name("EWK Herzogenbuchsee AG") == "EWK Herzogenbuchsee"
    assert core_name("Sparkonto (Hanna)") == "Sparkonto"
    assert same_company("EWK", "EWK Hezogenbuchsee AG")
    assert same_company("die mobiliar", "Die Mobiliar AG")
    assert not same_company("Post", "PostFinance")


def test_index_and_text_matching():
    idx = index()
    assert sorted(c.label for c in idx.companies) == [
        "_Companies/EWK Hezogenbuchsee AG", "_Companies/Post", "_Companies/Salt",
        "_Finanzinstitute/PostFinance", "_Versicherungen/Mobiliar",
    ]
    # acronym matches although the folder name has a typo; "PostFinance" is not "Post"
    found = idx.in_text("EWK Herzogenbuchsee AG\nStromrechnung\nZahlbar via PostFinance")
    assert [c.name for c in found] == ["EWK Hezogenbuchsee AG", "PostFinance"]
    ewk = found[0]
    assert idx.best_label(ewk) == "_Companies/EWK Hezogenbuchsee AG/Stromrechnungen/{year}"
    assert idx.in_text("Salz und Pfeffer") == []


def test_company_proposal_without_model():
    out = apply_companies([], index(), text="Salt Mobile SA\nRechnung", sender=None, top_n=3)
    assert [(p.label, p.source) for p in out] == [("_Companies/Salt", "sender")]


def test_model_decides_between_names_on_the_page():
    text = "P.P. CH-3360 Herzogenbuchsee Post CH AG\nDie Mobiliar\nPolice"
    preds = model(("_Steuern/Steuerrechnungen/{year}", 0.3), ("_Versicherungen/Mobiliar/{year}", 0.25))
    out = apply_companies(preds, index(), text=text, sender=None, top_n=3)
    assert out[0].label == "_Versicherungen/Mobiliar/{year}" and out[0].source == "model"
    assert len(out) == 2


def test_confident_model_stays_first():
    preds = model(("_Steuern/Steuerrechnungen/{year}", 0.8), ("_Companies/Post/Zolldokumente", 0.1))
    out = apply_companies(preds, index(), text="Die Mobiliar", sender=None, top_n=3)
    assert [p.label for p in out] == [
        "_Steuern/Steuerrechnungen/{year}", "_Versicherungen/Mobiliar/{year}", "_Companies/Post/Zolldokumente",
    ]


def test_known_sender_and_new_company():
    idx = index()
    out = apply_companies([], idx, text="", sender="EWK", top_n=3)
    assert out[0].label == "_Companies/EWK Hezogenbuchsee AG/Stromrechnungen/{year}"

    preds = model(("_Companies/Salt", 0.4), ("_Versicherungen/Mobiliar/{year}", 0.3), ("_Companies/Post", 0.2))
    out = apply_companies(preds, idx, text="Swisscom", sender="Swisscom", top_n=3,
                          new_company_folder="_Companies")
    assert [(p.label, p.source) for p in out] == [
        ("_Companies/Salt", "model"), ("_Versicherungen/Mobiliar/{year}", "model"), ("_Companies/Swisscom", "new"),
    ]
    # rules always come first
    rule = [Prediction("_Steuern/Steuerrechnungen/{year}", 1.0, "rule")]
    out = apply_companies(rule, idx, text="Salt", sender=None, top_n=3)
    assert [p.source for p in out] == ["rule", "sender"]


def test_year_anywhere_and_include():
    rules = LabelRules("/Dokumente", r"^\d{4}$", [], ["/Dokumente/_Review"], include=["_*"])
    assert rules.label_for_path("/Dokumente/_Steuern/Steuererklärungen/2025/Dokumente") == \
        "_Steuern/Steuererklärungen/{year}/Dokumente"
    assert rules.label_for_path("/Dokumente/2024/Foo") == "2024/Foo"  # top level stays
    assert rules.is_label_included("_Companies/Salt")
    assert rules.is_label_untrained("CNC Jobs")
    assert not rules.is_label_excluded("CNC Jobs")  # still selectable by hand
    assert LabelRules("/Dokumente", r"^\d{4}$", [], []).is_label_included("CNC Jobs")
