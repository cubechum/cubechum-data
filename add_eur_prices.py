"""EUR árak hozzáadása a cards.sqlite-hoz (prices_eur tábla).

Források:
  - Cardmarket hivatalos árlistája (price_guide_6.json): idProduct -> árak, EUR.
  - TCGdex cards-database (MIT): kártyánként a thirdParty azonosítópár,
    TCGplayer productId <-> Cardmarket idProduct. A mi cards.product_id
    a TCGplayer productId, így a párosítás pontos, névtalálgatás nélkül.

Használat: python add_eur_prices.py [cards.sqlite]

Az adatbázist csak akkor módosítja, ha minden ellenőrzés átment (egy tranzakció).
Hiba esetén nem nyúl hozzá, és nem nulla kilépési kóddal áll meg; a workflow
ezt nem tekinti végzetesnek, az USD adatcsomag EUR nélkül is kimegy.
"""
import json
import re
import sqlite3
import sys
import tarfile
import urllib.request
from collections import defaultdict

UA = "CubeChumData/1.0 (github actions)"
PRICE_GUIDE_URL = (
    "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_6.json"
)
TCGDEX_TARBALL = (
    "https://codeload.github.com/tcgdex/cards-database/tar.gz/refs/heads/master"
)

# Védelem: egy csonka letöltés vagy megváltozott formátum ne írjon rossz adatot.
MIN_GUIDE_ROWS = 50_000
MIN_PAIRS = 15_000
MIN_MATCHED = 15_000

# "Gyanús" jelzés: az EUR ár a TCGplayer ársáv 0,3-szorosa alatt vagy 3-szorosa
# felett van (csak 2 EUR/USD fölött érdemes arányt nézni).
SUSPECT_RATIO_LOW = 0.3
SUSPECT_RATIO_HIGH = 3.0
SUSPECT_MIN_VALUE = 2.0

# Cardmarket mező -> oszlopnév. A "-holo" mezők a foil (csillogó) árak.
FIELDS = {
    "low": "low",
    "trend": "trend",
    "avg": "avg",
    "avg1": "avg1",
    "avg7": "avg7",
    "avg30": "avg30",
    "low-holo": "low_holo",
    "trend-holo": "trend_holo",
    "avg-holo": "avg_holo",
    "avg1-holo": "avg1_holo",
    "avg7-holo": "avg7_holo",
    "avg30-holo": "avg30_holo",
}

THIRD_PARTY = re.compile(r"thirdParty:\s*\{([^}]*)\}", re.S)
ID = re.compile(r"(tcgplayer|cardmarket):\s*(\d+)")


def fetch(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=180)


def load_price_guide() -> tuple[dict[int, dict], str]:
    with fetch(PRICE_GUIDE_URL) as r:
        data = json.load(r)
    rows = data["priceGuides"]
    if len(rows) < MIN_GUIDE_ROWS:
        raise SystemExit(f"Gyanúsan kevés Cardmarket sor: {len(rows)}")
    guide = {row["idProduct"]: row for row in rows}
    return guide, str(data.get("createdAt") or "")


def load_pairs() -> dict[int, set[int]]:
    """TCGplayer productId -> a hozzá tartozó Cardmarket idProduct-ok."""
    pairs: dict[int, set[int]] = defaultdict(set)
    with fetch(TCGDEX_TARBALL) as r, tarfile.open(fileobj=r, mode="r|gz") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".ts"):
                continue
            if "/data/" not in member.name:
                continue
            text = tar.extractfile(member).read().decode("utf-8", "ignore")
            if "thirdParty" not in text:
                continue
            for block in THIRD_PARTY.findall(text):
                ids = dict(ID.findall(block))
                if "tcgplayer" in ids and "cardmarket" in ids:
                    pairs[int(ids["tcgplayer"])].add(int(ids["cardmarket"]))
    if len(pairs) < MIN_PAIRS:
        raise SystemExit(f"Gyanúsan kevés TCGdex páros: {len(pairs)}")
    return pairs


def has_price(row: dict) -> bool:
    return any(row.get(f) for f in ("trend", "low", "avg"))


def signature(row: dict) -> tuple:
    return tuple(row.get(f) for f in FIELDS)


def is_suspect(row: dict, usd_range: tuple | None) -> int:
    """1, ha az EUR ár aránytalanul eltér a TCGplayer ársávtól (USD).

    A TCGdex azonosítói között van néhány hibás (más nyomtatásra vagy másik
    kártyára mutat), ezt a nagy eltérés árulja el. Az app dönthet, hogy az ilyet
    jelzéssel mutatja vagy elrejti. Kis összegeknél nincs értelme az aránynak.
    """
    eur = row.get("trend") or row.get("avg") or row.get("low")
    if not eur or not usd_range:
        return 0
    lo, hi = usd_range
    if max(eur, hi) < SUSPECT_MIN_VALUE:
        return 0
    return int(eur < SUSPECT_RATIO_LOW * lo or eur > SUSPECT_RATIO_HIGH * hi)


def main() -> None:
    # Windows konzolon (cp1252) a magyar ő/ű kiírása különben hibát dobna.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    path =sys.argv[1] if len(sys.argv) > 1 else "cards.sqlite"

    guide, guide_date = load_price_guide()
    print(f"Cardmarket árlista: {len(guide)} sor, {guide_date}")
    pairs = load_pairs()
    print(f"TCGdex párosítás: {len(pairs)} TCGplayer azonosító")

    db = sqlite3.connect(path)
    products = [r[0] for r in db.execute("SELECT product_id FROM cards")]

    # A TCGplayer ársáv kártyánként (USD): ehhez mérjük az EUR árat.
    usd = {
        pid: (lo, hi)
        for pid, lo, hi in db.execute(
            "SELECT product_id, MIN(market), MAX(market) FROM prices "
            "WHERE market > 0 GROUP BY product_id"
        )
    }

    out = []
    ambiguous = 0
    suspects = 0
    for pid in products:
        candidates = [
            guide[c] for c in pairs.get(pid, ()) if c in guide and has_price(guide[c])
        ]
        if not candidates:
            continue
        # Több eltérő árú Cardmarket termék: nem tudjuk melyik a mienk, kihagyjuk.
        if len({signature(c) for c in candidates}) > 1:
            ambiguous += 1
            continue
        row = candidates[0]
        suspect = is_suspect(row, usd.get(pid))
        suspects += suspect
        out.append((pid, row["idProduct"], suspect, *[row.get(f) for f in FIELDS]))

    print(
        f"Kártyák: {len(products)} | EUR árral: {len(out)} "
        f"({100 * len(out) / max(len(products), 1):.1f}%) | félreérthető, kihagyva: {ambiguous} "
        f"| gyanús (EUR és USD ár nagyon eltér): {suspects}"
    )
    if len(out) < MIN_MATCHED:
        raise SystemExit(f"Gyanúsan kevés párosított kártya: {len(out)}")

    cols = ", ".join(f"{c} REAL" for c in FIELDS.values())
    with db:  # egy tranzakció: vagy minden bekerül, vagy semmi
        db.execute("DROP TABLE IF EXISTS prices_eur")
        db.execute(
            "CREATE TABLE prices_eur ("
            "product_id INTEGER PRIMARY KEY, cm_product_id INTEGER, "
            "suspect INTEGER, " + cols + ")"
        )
        marks = ",".join("?" * (3 + len(FIELDS)))
        db.executemany(f"INSERT INTO prices_eur VALUES ({marks})", out)
        db.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            [
                ("eur_date", guide_date),
                ("eur_count", str(len(out))),
                ("eur_source", "Cardmarket price guide, TCGdex thirdParty ids"),
            ],
        )
    db.close()
    print("prices_eur kész.")


if __name__ == "__main__":
    main()
