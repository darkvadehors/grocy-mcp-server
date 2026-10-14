import pytest

from grocy_mcp.packaging import AMBIGUOUS, Content, metric_kind, parse_content, plan_product

UNITS = {
    1: "Kilogramme", 2: "Pièce", 3: "Pack", 4: "Gramme", 5: "Litre", 6: "Centilitre", 7: "Millilitre",
    8: "Paquet", 9: "Bouteille",
}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Riz Noir (Carrefour) 500 g", Content(1, 500, "Gramme")),
        ("Sucre Béghin say 500 g", Content(1, 500, "Gramme")),
        ("KITKAT barre chocolatée 41,5g", Content(1, 41.5, "Gramme")),
        ("La Blanche (Brasserie du Mont Blanc) 75 cl", Content(1, 75, "Centilitre")),
        ("NESCAFÉ® KitKat Latte, Café soluble, Boîte de 273g", Content(1, 273, "Gramme")),
        ("Viande des grisons (Marque Repère) 80 g (10 tr.)", Content(1, 80, "Gramme")),
        ("Jus de pomme 1,5 L", Content(1, 1.5, "Litre")),
        ("Farine 1 kg", Content(1, 1, "Kilogramme")),
        ("Sel 500 grammes", Content(1, 500, "Gramme")),
        ("Fellowes screen Cleaning Spray - 250 ml", Content(1, 250, "Millilitre")),
        ("Tourtel 6X27,5CL TOURTEL TW CITRON 0.0 DEGRE ALCOOL 1650 ml", Content(6, 27.5, "Centilitre")),
        ("Yaourts 4 x 125 g", Content(4, 125, "Gramme")),
        ("Pâtes 500 g 0,5 kg", Content(1, 500, "Gramme")),  # deux écritures de la même contenance
    ],
)
def test_parse_content(text, expected):
    assert parse_content(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Morceaux de noix",
        "BiC Velleda Whiteboard Marker Assorted Colours",
        "Bière 0.0 degré",
        "Lot de 4",
        "",
        None,
    ],
)
def test_parse_content_absent(text):
    assert parse_content(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Pâtes 500 g 1 kg",  # Review Focus 1 : deux contenances discordantes
        "Tourtel 6X27,5CL 1500 ml",  # lot dont le total ne correspond pas
        "Lait 1 L 500 g",  # masse et volume mêlés
    ],
)
def test_parse_content_ambiguous(text):
    assert parse_content(text) is AMBIGUOUS


def test_metric_kind():
    assert metric_kind("Kilogramme") == ("mass", 1000)
    assert metric_kind("gramme") == ("mass", 1)
    assert metric_kind("Centilitre") == ("volume", 10)
    assert metric_kind("Mètre cube") == ("volume", 1_000_000)
    assert metric_kind("Pièce") is None and metric_kind("Pack") is None


def product(pid=1, name="Riz 500 g", stock=2, purchase=None):
    return {"id": pid, "name": name, "qu_id_stock": stock, "qu_id_purchase": purchase or stock, "qu_id_price": purchase or stock}


def test_plan_piece_product_gets_its_content():
    decision = plan_product(product(), UNITS, [], [("nom", Content(1, 500, "Gramme"))])
    assert decision.status == "a_ecrire"
    assert decision.conversions == [(2, 4, 500)]
    assert decision.purchase_unit is None


def test_plan_weight_product_gets_packet_and_piece():
    """Produit stocké en kg, contenance 250 g : 1 Paquet = 1 Pièce = 0,25 kg, achat au paquet."""
    decision = plan_product(product(name="Graines 250 g", stock=1), UNITS, [], [("nom", Content(1, 250, "Gramme"))])
    assert decision.status == "a_ecrire"
    assert sorted(decision.conversions) == [(2, 1, 0.25), (8, 1, 0.25)]
    assert decision.purchase_unit == 8


def test_plan_weight_product_keeps_a_chosen_purchase_unit():
    decision = plan_product(product(name="Graines 250 g", stock=1, purchase=3), UNITS, [], [("nom", Content(1, 250, "Gramme"))])
    assert decision.status == "a_ecrire" and decision.purchase_unit is None


def test_plan_never_touches_a_product_with_its_own_conversion():
    """Review Focus 3 : une conversion propre (manuelle ou d'un passage précédent) bloque toute écriture."""
    own = [{"product_id": 1, "from_qu_id": 3, "to_qu_id": 9, "factor": 6}]
    decision = plan_product(product(), UNITS, own, [("nom", Content(1, 500, "Gramme"))])
    assert decision.status == "deja" and decision.conversions == []


def test_plan_multipack_is_listed_not_guessed():
    """Review Focus 2."""
    decision = plan_product(product(name="Tourtel 6X27,5CL"), UNITS, [], [("nom", Content(6, 27.5, "Centilitre"))])
    assert decision.status == "lot" and decision.conversions == []


def test_plan_sources_must_agree():
    decision = plan_product(product(), UNITS, [], [("nom", Content(1, 500, "Gramme")), ("Open Food Facts", Content(1, 1, "Kilogramme"))])
    assert decision.status == "contradictoire" and decision.conversions == []
    agree = plan_product(product(), UNITS, [], [("nom", Content(1, 500, "Gramme")), ("Open Food Facts", Content(1, 0.5, "Kilogramme"))])
    assert agree.status == "a_ecrire" and agree.source == "nom"


def test_plan_ambiguous_source_blocks():
    decision = plan_product(product(), UNITS, [], [("nom", AMBIGUOUS), ("Open Food Facts", Content(1, 500, "Gramme"))])
    assert decision.status == "contradictoire"


def test_plan_mass_and_volume_never_mix():
    """Review Focus 5 : produit stocké en kg, contenance en cl."""
    decision = plan_product(product(name="Sirop 75 cl", stock=1), UNITS, [], [("nom", Content(1, 75, "Centilitre"))])
    assert decision.status == "incompatible" and decision.conversions == []


def test_plan_unknown_without_source():
    assert plan_product(product(name="Morceaux de noix"), UNITS, [], []).status == "inconnu"


def test_plan_missing_packet_unit_still_writes_piece():
    units = {k: v for k, v in UNITS.items() if v != "Paquet"}
    decision = plan_product(product(name="Graines 250 g", stock=1), units, [], [("nom", Content(1, 250, "Gramme"))])
    assert decision.conversions == [(2, 1, 0.25)] and decision.purchase_unit is None


def test_plan_ignores_other_products_conversions():
    other = [{"product_id": 9, "from_qu_id": 2, "to_qu_id": 4, "factor": 80}, {"product_id": None, "from_qu_id": 1, "to_qu_id": 4, "factor": 1000}]
    assert plan_product(product(), UNITS, other, [("nom", Content(1, 500, "Gramme"))]).status == "a_ecrire"
