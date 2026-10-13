"""Génère src/grocy_mcp/data/ciqual-2020.json depuis la table Ciqual officielle (Anses, Licence Ouverte Etalab).

Usage : python scripts/build_ciqual.py <XML_2020_07_07.zip>
Source : https://www.data.gouv.fr/datasets/table-de-composition-nutritionnelle-des-aliments-ciqual/
"""

import sys
from pathlib import Path

from grocy_mcp.nutrition import CIQUAL_PATH, build_ciqual

if __name__ == "__main__":
    archive = Path(sys.argv[1]) if len(sys.argv) > 1 else sys.exit(__doc__)
    print(f"{build_ciqual(archive, CIQUAL_PATH)} aliments écrits dans {CIQUAL_PATH}")
