import json
import zipfile

import pytest

from grocy_mcp.nutrition import (
    NUTRIENTS,
    CiqualIndex,
    build_ciqual,
    check_values,
    daily_needs,
    parse_household,
    parse_teneur,
    weight_in_grams,
)

DATA = "tests/data"


@pytest.mark.parametrize(
    ("raw", "value"),
    [("145", 145.0), ("2,91", 2.91), ("< 0,5", 0.0), ("traces", 0.0), ("-", None), ("", None), ("abc", None)],
)
def test_parse_teneur(raw, value):
    assert parse_teneur(raw) == value


def test_build_ciqual_et_recherche(tmp_path):
    """N5 : génération depuis le XML officiel (zip), puis recherche par nom."""
    archive = tmp_path / "ciqual.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("alim_2020_07_07.xml", open(f"{DATA}/ciqual-extrait.xml.txt", encoding="utf-8").read().encode("cp1252"))
        z.writestr("compo_2020_07_07.xml", open(f"{DATA}/ciqual-compo.xml.txt", encoding="utf-8").read().encode("cp1252"))
    out = tmp_path / "ciqual.json"
    build_ciqual(archive, out)
    foods = json.loads(out.read_text())
    rice = next(f for f in foods if f["code"] == 9100)
    assert rice["nom"] == "Riz blanc, cuit, non salé"
    assert rice["kcal"] == 145.0 and rice["proteines"] == 2.91 and rice["sucres"] == 0.0 and rice["ags"] == 0.0
    assert "fibres" not in rice  # « - » : valeur absente
    assert next(f for f in foods if f["code"] == 5004)["nom"] == "Panaché préemballé (<1° alc.)"  # « < » non échappé (fichier réel)
    index = CiqualIndex(out)
    assert [f["code"] for f in index.search("riz blanc cuit")] == [9100]
    assert {f["code"] for f in index.search("riz")} == {9100, 9101}
    assert index.search("chorizo") == [] and index.get(13000)["nom"].startswith("Pomme")


def test_ciqual_embarque():
    index = CiqualIndex()
    assert len(index) > 3000
    assert index.search("lentille", limit=3)


def values(**overrides):
    base = {"kcal": 350, "proteines": 10, "glucides": 70, "sucres": 5, "lipides": 2, "ags": 0.5, "fibres": 3, "sel": 0.1}
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "bad",
    [
        {"kcal": 4000},
        {"kcal": -1},
        {"sucres": 80},  # > glucides
        {"ags": 3},  # > lipides
        {"proteines": 101},
        {"glucides": 90, "proteines": 20},  # somme > 105 g
    ],
)
def test_vraisemblance_refuse(bad):
    """N3 : une proposition aberrante est refusée avec une raison."""
    assert check_values(values(**bad))


def test_vraisemblance_accepte():
    assert check_values(values()) is None
    assert check_values({"kcal": 0, "sel": 0}) is None
    assert set(NUTRIENTS) == {"kcal", "proteines", "glucides", "sucres", "lipides", "ags", "fibres", "sel"}


@pytest.mark.parametrize(
    ("amount", "unit", "grams", "approx"),
    [(500, "g", 500, False), (1.5, "kg", 1500, False), (33, "cl", 330, True), (1, "l", 1000, True), (250, "ml", 250, True)],
)
def test_poids_converti(amount, unit, grams, approx):
    assert weight_in_grams(amount, unit) == (grams, approx)


@pytest.mark.parametrize(("amount", "unit"), [(0, "g"), (60000, "g"), (1, "pièce"), (None, "g")])
def test_poids_refuse(amount, unit):
    assert weight_in_grams(amount, unit) is None


def test_foyer_et_besoins():
    household = parse_household("H:50,F:54,F:20")
    assert household == [("H", 50), ("F", 54), ("F", 20)]
    assert daily_needs(household) == {"kcal": 6600, "proteines": 158}
    assert daily_needs(parse_household("H:25, F:75")) == {"kcal": 4500, "proteines": 108}
    for bad in ("X:30", "H:10", "H:abc", "", "H:50;F:54"):
        with pytest.raises(ValueError):
            parse_household(bad)


def test_classement_ciqual_reel():
    """Le premier candidat est l'aliment lui-même, avec des valeurs, pas un plat qui le contient."""
    index = CiqualIndex()
    first = index.search("lentilles")[0]
    assert first["nom"].lower().startswith("lentille") and "kcal" in first
    assert index.search("riz blanc cuit")[0]["nom"] == "Riz blanc, cuit, non salé"
    assert index.search("pomme crue")[0]["nom"].lower().startswith("pomme") and "kcal" in index.search("pomme crue")[0]
