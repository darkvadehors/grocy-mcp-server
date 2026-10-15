import pytest

from grocy_mcp.prices import (
    apply_discounts,
    best_split,
    check_total,
    latest_by_store,
    match_store,
    normalized_price,
    price_rises,
    stock_amount,
    suggest_products,
)

UNITS = {1: "Pièce", 2: "Kilogramme", 3: "Gramme", 4: "Paquet", 5: "Litre"}
STORES = [{"id": 2, "name": "Leclerc"}, {"id": 3, "name": "Intermarché"}, {"id": 4, "name": "Leclerc Drive"}]


def product(pid=1, stock=1, purchase=None, **extra):
    return {"id": pid, "name": "Riz", "qu_id_stock": stock, "qu_id_purchase": purchase or stock, "userfields": {}, **extra}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Leclerc", 2),
        ("LECLERC LAON", 2),
        ("Intermarche Super Laon", 3),
        ("leclerc drive laon", 4),  # le nom le plus long l'emporte
        ("Grand Frais", None),
        ("Lec", None),
        ("Leclercville", None),  # « Leclerc » doit être suivi d'une espace
        ("", None),
    ],
)
def test_match_store(name, expected):
    found = match_store(STORES, name)
    assert (found["id"] if found else None) == expected


def test_discount_goes_to_previous_article():
    lines = [
        {"ligne": 1, "libelle": "BEURRE", "prix": 2.5},
        {"ligne": 2, "libelle": "REMISE", "prix": -0.5},
        {"ligne": 3, "libelle": "LAIT", "prix": 1.2},
    ]
    articles, warnings = apply_discounts(lines)
    assert [(a["ligne"], a["prix"], a["remise"]) for a in articles] == [(1, 2.0, 0.5), (3, 1.2, 0)]
    assert warnings == []


def test_discount_without_article_or_too_big_is_not_applied():
    articles, warnings = apply_discounts([{"ligne": 1, "libelle": "BON", "prix": -1}, {"ligne": 2, "libelle": "PAIN", "prix": 1}])
    assert [a["prix"] for a in articles] == [1] and "ligne 1" in warnings[0]
    articles, warnings = apply_discounts([{"ligne": 1, "libelle": "PAIN", "prix": 1}, {"ligne": 2, "libelle": "REM", "prix": -3}])
    assert articles[0]["prix"] == 1 and "ligne 2" in warnings[0]


def test_check_total():
    lines = [{"prix": 2.5}, {"prix": -0.5}, {"prix": 1.2}]
    assert check_total(lines, 3.2) == 0
    assert check_total(lines, 3.25) == -0.05
    assert check_total(lines, 10) == -6.8


def test_suggest_products_by_words_and_prefixes():
    products = [
        {"id": 1, "name": "Champignons de Paris pieds et morceaux"},
        {"id": 2, "name": "Jus de pomme 100% pur jus"},
        {"id": 3, "name": "Beurre doux"},
        {"id": 4, "name": "Paris Brest"},
    ]
    assert [p["id"] for p in suggest_products("CHAMP. PARIS 400G", products)] == [1, 4]
    assert [p["id"] for p in suggest_products("JUS POMME", products)] == [2]
    assert suggest_products("SAC CABAS", products) == []
    assert suggest_products("de", products) == []  # mots de moins de 3 lettres ignorés


@pytest.mark.parametrize(
    ("line", "prod", "conversions", "expected"),
    [
        ({"quantite": 2}, product(stock=1), [], 2),
        ({"poids_kg": 0.5}, product(stock=2), [], 0.5),
        ({"poids_kg": 0.5}, product(stock=3), [], 500),
        ({"poids_kg": 0.5}, product(stock=1), [{"product_id": 1, "from_qu_id": 1, "to_qu_id": 3, "factor": 250}], 2),
        ({"poids_kg": 0.5}, product(stock=1), [], None),  # pièce sans contenance : rien d'inventé
        ({"quantite": 3}, product(stock=1, purchase=4), [{"product_id": 1, "from_qu_id": 4, "to_qu_id": 1, "factor": 6}], 18),
        ({"quantite": 2}, product(stock=4, purchase=1), [{"product_id": 1, "from_qu_id": 4, "to_qu_id": 1, "factor": 4}], 0.5),
        ({"quantite": 1}, product(stock=1, purchase=4), [], None),  # unité d'achat sans conversion
        ({"quantite": 1}, product(stock=1, purchase=4), [{"product_id": None, "from_qu_id": 4, "to_qu_id": 1, "factor": 10}], 10),
    ],
)
def test_stock_amount(line, prod, conversions, expected):
    assert stock_amount(line, prod, UNITS, conversions) == expected


def test_normalized_price():
    assert normalized_price(1.75, product(stock=1), UNITS, [{"product_id": 1, "from_qu_id": 1, "to_qu_id": 3, "factor": 500}]) == {
        "valeur": 3.5, "unite": "kg"}
    assert normalized_price(0.9, product(stock=1), UNITS, [{"product_id": 1, "from_qu_id": 1, "to_qu_id": 5, "factor": 1.5}]) == {
        "valeur": 0.6, "unite": "l"}
    assert normalized_price(4, product(stock=2), UNITS, []) == {"valeur": 4, "unite": "kg"}
    assert normalized_price(2.2, product(stock=1), UNITS, []) == {"valeur": 2.2, "unite": "Pièce"}


def test_latest_by_store():
    rows = [
        {"location_id": 2, "prix": 1.0, "date": "2026-09-01", "source": "achat"},
        {"location_id": 2, "prix": 1.2, "date": "2026-10-01", "source": "rayon"},
        {"location_id": 3, "prix": 0.9, "date": "2026-08-01", "source": "achat"},
        {"location_id": None, "prix": 5, "date": "2026-10-10", "source": "achat"},  # sans magasin : ignoré
    ]
    latest = latest_by_store(rows)
    assert set(latest) == {2, 3} and latest[2]["prix"] == 1.2 and latest[3]["date"] == "2026-08-01"


def test_best_split_coverage_then_price():
    items = {
        "riz": {2: 2.0, 3: 1.5},
        "lait": {2: 1.0, 3: 1.4},
        "pain": {5: 1.0},
    }
    one = best_split(items, 1)
    assert one["magasins"] == [3] and one["total"] == 2.9 and one["non_couverts"] == ["pain"]
    two = best_split(items, 2)
    # 3 + 5 couvre les trois articles : l'emporte sur 2 + 3 (deux articles), même plus chère
    assert two["magasins"] == [3, 5] and two["total"] == 3.9 and two["non_couverts"] == []
    assert two["affectation"] == {"riz": 3, "lait": 3, "pain": 5}
    assert two["reference"] is None  # aucun magasin seul ne couvre les trois articles
    three = best_split({**items, "sel": {2: 0.5}}, 3)
    assert three["magasins"] == [2, 3, 5] and three["affectation"]["lait"] == 2 and three["total"] == 4.0


def test_best_split_economy_on_same_items():
    items = {"riz": {2: 2.0, 3: 1.5}, "lait": {2: 1.0, 3: 1.4}, "the": {}}
    two = best_split(items, 2)
    assert two["total"] == 2.5 and two["magasins"] == [2, 3] and two["non_couverts"] == ["the"]
    assert two["reference"] == {"magasin": 3, "total": 2.9}  # meilleur magasin seul sur les mêmes articles
    assert best_split({}, 2) == {"magasins": [], "affectation": {}, "total": 0, "non_couverts": [], "reference": None}


def test_price_rises():
    rows = [
        {"product_id": 1, "location_id": 2, "date": "2026-08-01", "prix": 1.00},
        {"product_id": 1, "location_id": 2, "date": "2026-10-01", "prix": 1.15},
        {"product_id": 1, "location_id": 3, "date": "2026-10-01", "prix": 1.50},  # un seul prix : rien à comparer
        {"product_id": 2, "location_id": 2, "date": "2026-08-01", "prix": 2.00},
        {"product_id": 2, "location_id": 2, "date": "2026-10-01", "prix": 2.10},  # +5 % : sous le seuil
    ]
    assert price_rises(rows) == [
        {"product_id": 1, "location_id": 2, "avant": 1.0, "apres": 1.15, "hausse_pct": 15.0, "date": "2026-10-01"}
    ]
