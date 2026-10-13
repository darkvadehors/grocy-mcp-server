"""Apports nutritionnels : table Ciqual embarquée, contrôles de vraisemblance, poids, besoins du foyer (spec B)."""

from __future__ import annotations

import json
import re
import unicodedata
import zipfile
from pathlib import Path

# Nutriments de la déclaration nutritionnelle européenne, pour 100 g, et leur code de constituant Ciqual
NUTRIENTS = {
    "kcal": 328,
    "proteines": 25000,
    "glucides": 31000,
    "sucres": 32000,
    "lipides": 40000,
    "ags": 40302,
    "fibres": 34100,
    "sel": 10004,
}
CIQUAL_PATH = Path(__file__).parent / "data" / "ciqual-2020.json"
MIN_UNIT_GRAMS, MAX_UNIT_GRAMS = 1, 50_000
MAX_KCAL = 905  # huiles : 899 à 901 kcal pour 100 g dans Ciqual
ROUNDING = 1.0  # g

# Besoins de référence (EFSA, activité modérée NAP 1,6, arrondis) ; protéines : 0,83 g/kg × 70 kg (H) ou 60 kg (F)
_KCAL = {"H": ((18, 2700), (30, 2500), (60, 2300), (70, 2100)), "F": ((18, 2100), (30, 2000), (60, 1900), (70, 1800))}
_PROTEINS = {"H": 58, "F": 50}


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(ch for ch in text if not unicodedata.combining(ch)).split())


def parse_teneur(raw: str | None) -> float | None:
    """Teneur Ciqual : « 2,91 » → 2.91 ; « < 0,5 » et « traces » → 0 ; « - » ou illisible → absent."""
    text = (raw or "").strip().lower()
    if text.startswith("<") or text == "traces":
        return 0.0
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _field(block: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    return match.group(1).strip() if match else None


def build_ciqual(archive: Path | str, out: Path | str) -> int:
    """XML officiel de Ciqual (zip data.gouv.fr) → JSON compact {code, nom, nutriments}.

    Le fichier officiel n'est pas du XML bien formé (« < » non échappés : « (<1° alc.) », « < 0,5 ») :
    lecture tolérante par blocs plutôt qu'avec un analyseur XML.
    """
    by_const = {code: name for name, code in NUTRIENTS.items()}
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        alim = next(n for n in names if re.match(r"alim_\d", n))
        compo = next(n for n in names if n.startswith("compo_"))
        alim_text = z.read(alim).decode("cp1252")
        compo_text = z.read(compo).decode("cp1252")
    foods: dict[int, dict] = {}
    for block in re.findall(r"<ALIM>(.*?)</ALIM>", alim_text, re.S):
        code, name = _field(block, "alim_code"), _field(block, "alim_nom_fr")
        if code and code.isdigit() and name:
            foods[int(code)] = {"code": int(code), "nom": " ".join(name.split())}
    for block in re.findall(r"<COMPO>(.*?)</COMPO>", compo_text, re.S):
        code, const = _field(block, "alim_code"), _field(block, "const_code")
        if not (code and const and code.isdigit() and const.isdigit()):
            continue
        name = by_const.get(int(const))
        if name and int(code) in foods:
            value = parse_teneur(_field(block, "teneur"))
            if value is not None:
                foods[int(code)][name] = value
    rows = sorted(foods.values(), key=lambda f: f["code"])
    Path(out).write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(rows)


class CiqualIndex:
    def __init__(self, path: Path | str = CIQUAL_PATH) -> None:
        self._foods = json.loads(Path(path).read_text(encoding="utf-8"))
        self._by_code = {f["code"]: f for f in self._foods}
        self._words = [re.findall(r"\w+", _fold(f["nom"])) for f in self._foods]

    def __len__(self) -> int:
        return len(self._foods)

    def get(self, code: int) -> dict | None:
        return self._by_code.get(code)

    def search(self, text: str, limit: int = 3) -> list[dict]:
        """Aliments dont le nom contient chaque mot cherché en début de mot (« lentille » trouve « Lentilles »)."""
        # pluriel toléré : « lentilles » trouve « Lentille », « noix » reste « noix » (mot court)
        wanted = [w[:-1] if len(w) > 4 and w[-1] in "sx" else w for w in re.findall(r"\w+", _fold(text or ""))]
        if not wanted:
            return []
        found = [
            (
                "kcal" not in food,  # d'abord les aliments qui ont des valeurs
                not words[0].startswith(wanted[0]),  # puis ceux dont le nom commence par le mot cherché
                len(words),  # puis les noms les plus courts (l'aliment plutôt qu'un plat qui le contient)
                food["code"],
                food,
            )
            for food, words in zip(self._foods, self._words, strict=True)
            if words and "kcal" in food and all(any(w.startswith(q) for w in words) for q in wanted)
        ]
        return [t[-1] for t in sorted(found, key=lambda t: t[:4])[:limit]]


def check_values(values: dict) -> str | None:
    """Vraisemblance d'apports pour 100 g ; renvoie la raison d'un refus, ou None."""
    clean = {k: v for k, v in values.items() if k in NUTRIENTS and v is not None}
    for key, value in clean.items():
        if not isinstance(value, int | float) or isinstance(value, bool) or value != value or value < 0:
            return f"{key} : valeur invalide"
        if key == "kcal" and value > MAX_KCAL:
            return f"énergie supérieure à {MAX_KCAL} kcal pour 100 g"
        if key != "kcal" and value > 100:
            return f"{key} supérieur à 100 g pour 100 g"
    # tolérance d'arrondi : les tables publient « < 0,5 » (ramené à 0) à côté de petites valeurs mesurées
    if clean.get("sucres", 0) > clean.get("glucides", float("inf")) + ROUNDING:
        return "sucres supérieurs aux glucides"
    if clean.get("ags", 0) > clean.get("lipides", float("inf")) + ROUNDING:
        return "acides gras saturés supérieurs aux lipides"
    if sum(clean.get(k, 0) for k in ("proteines", "glucides", "lipides", "fibres")) > 105:
        return "somme protéines + glucides + lipides + fibres supérieure à 105 g pour 100 g"
    return None


def weight_in_grams(amount: object, unit: str | None) -> tuple[float, bool] | None:
    """Contenance → (grammes, approximatif ?) ; ml, cl et l comptés à 1 g/ml. None si inconnue ou hors bornes."""
    factors = {"g": (1, False), "gr": (1, False), "kg": (1000, False), "mg": (0.001, False),
               "ml": (1, True), "cl": (10, True), "dl": (100, True), "l": (1000, True)}
    if not isinstance(amount, int | float) or isinstance(amount, bool):
        return None
    factor = factors.get((unit or "").strip().lower())
    if factor is None:
        return None
    grams = round(float(amount) * factor[0], 3)
    if not MIN_UNIT_GRAMS <= grams <= MAX_UNIT_GRAMS:
        return None
    return (int(grams) if grams == int(grams) else grams), factor[1]


def parse_household(text: str) -> list[tuple[str, int]]:
    """« H:50,F:54,F:20 » → [("H", 50), ("F", 54), ("F", 20)] ; adultes de 18 ans et plus uniquement."""
    people = []
    for part in (text or "").split(","):
        match = re.fullmatch(r"\s*([HF])\s*:\s*(\d{1,3})\s*", part)
        if not match or not 18 <= int(match.group(2)) <= 120:
            raise ValueError("MCP_HOUSEHOLD : format attendu « H:50,F:54 » (adultes de 18 ans et plus)")
        people.append((match.group(1), int(match.group(2))))
    return people


def daily_needs(household: list[tuple[str, int]]) -> dict:
    kcal = proteins = 0
    for sex, age in household:
        kcal += next(value for start, value in reversed(_KCAL[sex]) if age >= start)
        proteins += _PROTEINS[sex]
    return {"kcal": kcal, "proteines": proteins}
