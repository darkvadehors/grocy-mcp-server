import pytest

from grocy_mcp.enrich import image_kind, missing_fields, pick_group, plan_enrichment, unit_weight, valid_image_url

UNITS = {1: "Pièce", 2: "Kilogramme", 3: "Gramme", 4: "Paquet", 5: "Centilitre", 6: "Litre"}
GROUPS = [{"id": 28, "name": "06.01 — Riz, pâtes, semoules et céréales"}, {"id": 6, "name": "01.01 — Eau potable"},
          {"id": 45, "name": "06.18 — Boissons, café, thé et infusions"}]
RULES = [["en:spring-waters", "01.01"], ["en:waters", "01.01"], ["en:beverages", "06.18"], ["en:rices", "06.01"]]
RECORD = {
    "q": "500 g",
    "v": {"kcal": 350, "proteines": 7, "glucides": 77, "sucres": 0.5, "lipides": 1, "sel": 0.01},
    "img": "https://images.openfoodfacts.org/images/products/300/000/000/0002/front_fr.12.400.jpg",
    "cats": ["en:plant-based-foods", "en:cereals-and-their-products", "en:rices"],
}


def product(pid=1, stock=1, **extra):
    return {"id": pid, "name": "Riz", "qu_id_stock": stock, "picture_file_name": None, "product_group_id": None,
            "calories": None, "userfields": {}, **extra}


def test_missing_fields_only_empty_and_never_done():
    assert missing_fields(product(), set()) == ["categorie", "nutrition", "photo"]
    full = product(picture_file_name="riz.jpg", product_group_id=28, userfields={"nutri_kcal_100g": "350"})
    assert missing_fields(full, set()) == []
    assert missing_fields(product(), {"photo", "categorie"}) == ["nutrition"]  # E2 : vidé par une personne, jamais repris
    assert "nutrition" not in missing_fields(product(calories="1750"), set())  # énergie saisie à la main
    assert "nutrition" not in missing_fields(product(userfields={"nutri_non_alimentaire": "1"}), set())


@pytest.mark.parametrize(
    ("prod", "conversions", "expected"),
    [
        (product(stock=2), [], (1000, False)),  # stocké en kg
        (product(stock=3), [], (1, False)),
        (product(stock=5), [], (10, True)),  # cl : 1 g/ml, approximatif
        (product(stock=1), [{"product_id": 1, "from_qu_id": 1, "to_qu_id": 3, "factor": 500}], (500, False)),
        (product(stock=1), [{"product_id": 1, "from_qu_id": 1, "to_qu_id": 6, "factor": 1.5}], (1500, True)),
        (product(stock=1, userfields={"nutri_poids_unite_g": "250"}), [], (250, False)),  # saisi par une personne
        (product(stock=1), [], None),  # Review Focus 2 : pièce sans contenance
        (product(stock=1), [{"product_id": 9, "from_qu_id": 1, "to_qu_id": 3, "factor": 500}], None),  # autre produit
        (product(stock=1), [{"product_id": 1, "from_qu_id": 1, "to_qu_id": 3, "factor": 500},
                            {"product_id": 1, "from_qu_id": 1, "to_qu_id": 2, "factor": 1}], None),  # discordantes
        (product(stock=1), [{"product_id": 1, "from_qu_id": 1, "to_qu_id": 2, "factor": 90}], None),  # 90 kg : hors bornes
        (product(stock=4), [{"product_id": 1, "from_qu_id": 1, "to_qu_id": 3, "factor": 500}], None),  # pas l'unité de stock
    ],
)
def test_unit_weight(prod, conversions, expected):
    assert unit_weight(prod, UNITS, conversions) == expected


def test_pick_group_first_rule_wins():
    assert pick_group(["en:beverages", "en:waters", "en:spring-waters"], RULES, GROUPS) == 6
    assert pick_group(["en:beverages", "en:sodas"], RULES, GROUPS) == 45
    assert pick_group(["en:snacks"], RULES, GROUPS) is None
    assert pick_group(["en:rices"], RULES, [g for g in GROUPS if g["id"] != 28]) is None  # catégorie absente de Grocy
    assert pick_group(["en:rices"], [], GROUPS) is None
    assert pick_group(["en:rices"], [["en:rices", "06.0"]], GROUPS) is None  # « 06.0 » n'est pas « 06.01 »


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://images.openfoodfacts.org/images/products/1/front.400.jpg", True),
        ("http://images.openfoodfacts.org/images/products/1/front.400.jpg", False),
        ("https://images.openfoodfacts.org.evil.test/x.jpg", False),
        ("https://user@images.openfoodfacts.org/x.jpg", False),
        ("https://images.openfoodfacts.org:8443/x.jpg", False),
        ("https://static.openfoodfacts.org/x.jpg", False),
        ("", False),
        (None, False),
        (42, False),
    ],
)
def test_valid_image_url(url, ok):
    assert valid_image_url(url) is ok


def test_image_kind():
    assert image_kind(b"\xff\xd8\xff\xe0" + b"0" * 20) == "jpg"
    assert image_kind(b"\x89PNG\r\n\x1a\n" + b"0" * 20) == "png"
    assert image_kind(b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"0" * 20) == "webp"
    assert image_kind(b"<svg xmlns=") is None and image_kind(b"") is None and image_kind(b"GIF89a") is None


def test_plan_fills_every_empty_field():
    conversions = [{"product_id": 1, "from_qu_id": 1, "to_qu_id": 3, "factor": 500}]
    plan = plan_enrichment(product(), RECORD, UNITS, conversions, GROUPS, RULES, set())
    assert plan.group_id == 28
    assert plan.image_url == RECORD["img"]
    assert plan.nutrition == {"valeurs": RECORD["v"], "poids": 500, "approximatif": False, "calories": 1750}
    assert plan.fields == ["categorie", "nutrition", "photo"]


def test_plan_never_invents_calories_without_weight():
    plan = plan_enrichment(product(), RECORD, UNITS, [], GROUPS, RULES, set())
    assert plan.nutrition is None and "nutrition" not in plan.fields and plan.reasons["nutrition"] == "poids inconnu"


@pytest.mark.parametrize(
    "values",
    [
        {"proteines": 7},  # sans énergie
        {"kcal": 2000},  # invraisemblable
        {"kcal": 300, "sucres": 50, "glucides": 10},
        {},
    ],
)
def test_plan_refuses_doubtful_nutrition(values):
    plan = plan_enrichment(product(stock=3), {**RECORD, "v": values}, UNITS, [], GROUPS, RULES, set())
    assert plan.nutrition is None


def test_plan_keeps_what_is_filled():
    filled = product(picture_file_name="a.jpg", product_group_id=6, userfields={"nutri_kcal_100g": "1"})
    plan = plan_enrichment(filled, RECORD, UNITS, [], GROUPS, RULES, set())
    assert plan.fields == [] and plan.image_url is None and plan.group_id is None and plan.nutrition is None


def test_plan_without_record_or_bad_image():
    assert plan_enrichment(product(), None, UNITS, [], GROUPS, RULES, set()).fields == []
    bad = plan_enrichment(product(stock=3), {**RECORD, "img": "https://evil.test/x.jpg"}, UNITS, [], GROUPS, RULES, set())
    assert bad.image_url is None and "photo" not in bad.fields


def test_plan_keeps_a_weight_typed_by_a_person():
    prod = product(stock=1, userfields={"nutri_poids_unite_g": "250"})
    plan = plan_enrichment(prod, RECORD, UNITS, [], GROUPS, RULES, set())
    assert plan.nutrition["poids"] == 250 and plan.nutrition["calories"] == 875
