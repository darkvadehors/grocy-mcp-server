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
        ("Jus de pomme 1,5 L", Content(1, 1.5, "Litre")),
        ("Farine 1 kg", Content(1, 1, "Kilogramme")),
        ("Sel 500 grammes", Content(1, 500, "Gramme")),
        ("Fellowes screen Cleaning Spray - 250 ml", Content(1, 250, "Millilitre")),
        ("Tourtel 6X27,5CL TOURTEL TW CITRON 0.0 DEGRE ALCOOL 1650 ml", Content(6, 27.5, "Centilitre")),
        ("Yaourts 4 x 125 g", Content(4, 125, "Gramme")),
        ("Coca-Cola 33cl x 6", Content(6, 33, "Centilitre")),
        ("Bière 25 cl x 12", Content(12, 25, "Centilitre")),
        ("Nutella B-ready 22g x 6", Content(6, 22, "Gramme")),
        ("Jus d'orange 100% pur jus 1 L", Content(1, 1, "Litre")),
        ("Crème 0% 20 cl", Content(1, 20, "Centilitre")),
        ("Noix paquet de 250 g", Content(1, 250, "Gramme")),
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
        "Evian 1L5",  # notation 1,5 L : jamais lue « 1 L »
        "Lait 1/2 L",  # jamais lu « 2 L »
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
        # C2 de la revue : un nombre d'éléments dans le nom rend la contenance incertaine
        "Viande des grisons (Marque Repère) 80 g (10 tr.)",
        "Eau 50 cl lot de 24",
        "Yaourts 4 pots de 125 g",
        "Lot de 4 yaourts 125 g",
        "Café 10 capsules 5,2 g",
        "Thon 3 boîtes x 80 g",
        "6 Oeufs frais 360 g",
        "Pack de 6 bouteilles 1,5 L",
        "Bière blonde 25 cl x",
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


def test_plan_completes_its_own_partial_write():
    """I1 : une écriture interrompue (Paquet créé, Pièce non) est complétée au passage suivant."""
    partial = [{"product_id": 1, "from_qu_id": 8, "to_qu_id": 1, "factor": 0.25}]
    decision = plan_product(product(name="Graines 250 g", stock=1), UNITS, partial, [("nom", Content(1, 250, "Gramme"))])
    assert decision.status == "a_ecrire" and decision.conversions == [(2, 1, 0.25)] and decision.purchase_unit == 8
    done = partial + [{"product_id": 1, "from_qu_id": 2, "to_qu_id": 1, "factor": 0.25}]
    finished = plan_product(product(name="Graines 250 g", stock=1, purchase=8), UNITS, done, [("nom", Content(1, 250, "Gramme"))])
    assert finished.status == "deja"


def test_plan_stock_unit_that_is_not_a_package():
    """I5 : un produit stocké à la tranche ne reçoit jamais « 1 Tranche = contenance du paquet »."""
    units = {**UNITS, 10: "Tranche"}
    decision = plan_product(product(stock=10), units, [], [("nom", Content(1, 80, "Gramme"))])
    assert decision.status == "unite_stock" and decision.conversions == []


def test_plan_ignores_other_products_conversions():
    other = [{"product_id": 9, "from_qu_id": 2, "to_qu_id": 4, "factor": 80}, {"product_id": None, "from_qu_id": 1, "to_qu_id": 4, "factor": 1000}]
    assert plan_product(product(), UNITS, other, [("nom", Content(1, 500, "Gramme"))]).status == "a_ecrire"
