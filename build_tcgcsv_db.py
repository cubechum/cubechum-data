"""
TCGplayer (tcgcsv.com) Pokémon adatok -> cards.sqlite

Használat (az AndroidPokemon mappában):
    python build_tcgcsv_db.py

Kimenet: cards.sqlite  (ezt másold a pokemon_scanner\\assets\\cards.sqlite helyére)

A tcgcsv irányelvei szerint: egyedi User-Agent, kérések között szünet,
naponta legfeljebb egyszer futtasd. A letöltött nyers fájlokat a
tcgcsv_cache mappába menti, így egy megszakadt futás újraindítása nem
tölt le mindent újra.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://tcgcsv.com"
CATEGORY = 3  # Pokemon
UA = "PokemonScanner/1.0 (personal project)"
OUT = Path("cards.sqlite")
CACHE_ROOT = Path("tcgcsv_cache")
SLEEP = 0.15

SCHEMA = """
CREATE TABLE sets (
  set_id INTEGER PRIMARY KEY,
  name TEXT,
  abbreviation TEXT,
  published_on TEXT
);
CREATE TABLE cards (
  product_id INTEGER PRIMARY KEY,
  set_id INTEGER,
  name TEXT,
  name_norm TEXT,
  number TEXT,
  number_int INTEGER,
  printed_total INTEGER,
  rarity TEXT,
  card_type TEXT,
  hp INTEGER,
  stage TEXT,
  weakness TEXT,
  resistance TEXT,
  retreat TEXT,
  text TEXT,
  image_url TEXT,
  url TEXT
);
CREATE TABLE prices (
  product_id INTEGER,
  sub_type TEXT,
  low REAL,
  mid REAL,
  high REAL,
  market REAL,
  direct_low REAL,
  PRIMARY KEY (product_id, sub_type)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX idx_cards_number ON cards(number_int, printed_total);
CREATE INDEX idx_cards_name ON cards(name_norm);
CREATE INDEX idx_cards_set ON cards(set_id);
"""

TAG = re.compile(r"<[^>]+>")


def fetch(url: str, cache_file: Path | None = None, retries: int = 4) -> bytes:
    if cache_file is not None and cache_file.exists():
        return cache_file.read_bytes()
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if cache_file is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(data)
            time.sleep(SLEEP)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b'{"results": []}'
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        wait = 2 * attempt
        print(f"  hiba ({last}), újrapróbálás {wait}s múlva ({attempt}/{retries})")
        time.sleep(wait)
    raise RuntimeError(f"Sikertelen letöltés: {url}: {last}")


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def clean(html: str | None) -> str:
    return re.sub(r"\s+", " ", TAG.sub(" ", html or "")).strip()


def first_int(s: str | None):
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else None


def parse_card(p: dict):
    ext = {}
    for e in p.get("extendedData") or []:
        if e.get("name") and e.get("value") is not None:
            ext[e["name"]] = str(e["value"])

    number = ext.get("Number")
    rarity = ext.get("Rarity")
    # Kártya = van sorszáma, vagy van ritkasága (a kódkártya nem fizikai kártya)
    if not number and (not rarity or rarity == "Code Card"):
        return None

    left, _, right = (number or "").partition("/")
    text_parts = [ext.get("CardText")] + [ext.get(f"Attack {i}") for i in range(1, 5)]
    text = " | ".join(clean(t) for t in text_parts if t)
    name = p["name"].strip()

    return (
        p["productId"], p["groupId"], name, fold(name), number,
        first_int(left), first_int(right), rarity, ext.get("Card Type"),
        first_int(ext.get("HP")), ext.get("Stage"), ext.get("Weakness"),
        ext.get("Resistance"), ext.get("RetreatCost"), text,
        p.get("imageUrl"), p.get("url"),
    )


def main() -> None:
    print("Verzió lekérése…")
    remote_date = fetch(f"{BASE}/last-updated.txt").decode().strip()
    print(f"Adatbázis dátuma (tcgcsv): {remote_date}")

    cache = CACHE_ROOT / re.sub(r"[^0-9A-Za-z]", "", remote_date)
    groups = json.loads(fetch(f"{BASE}/tcgplayer/{CATEGORY}/groups", cache / "groups.json"))["results"]
    print(f"{len(groups)} szett")

    tmp = OUT.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)

    total_cards = 0
    for i, g in enumerate(groups, 1):
        gid = g["groupId"]
        products = json.loads(
            fetch(f"{BASE}/tcgplayer/{CATEGORY}/{gid}/products", cache / f"{gid}_products.json")
        )["results"]
        prices = json.loads(
            fetch(f"{BASE}/tcgplayer/{CATEGORY}/{gid}/prices", cache / f"{gid}_prices.json")
        )["results"]

        rows = [r for r in (parse_card(p) for p in products) if r]
        ids = {r[0] for r in rows}
        if rows:
            db.execute(
                "INSERT OR REPLACE INTO sets VALUES (?,?,?,?)",
                (gid, g["name"], g.get("abbreviation"), g.get("publishedOn")),
            )
            db.executemany("INSERT OR REPLACE INTO cards VALUES (" + ",".join("?" * 17) + ")", rows)
            db.executemany(
                "INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?)",
                [
                    (p["productId"], p.get("subTypeName") or "Normal", p.get("lowPrice"),
                     p.get("midPrice"), p.get("highPrice"), p.get("marketPrice"),
                     p.get("directLowPrice"))
                    for p in prices if p["productId"] in ids
                ],
            )
        total_cards += len(rows)
        print(f"[{i}/{len(groups)}] {g['name']}: {len(rows)} kártya")
        db.commit()

    db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("schema", "2"),
        ("data_date", remote_date),
        ("built_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ("source", "tcgcsv.com / TCGplayer (USD)"),
        ("card_count", str(total_cards)),
    ])
    db.commit()
    db.execute("VACUUM")

    cards, with_price = db.execute(
        "SELECT (SELECT COUNT(*) FROM cards), (SELECT COUNT(DISTINCT product_id) FROM prices WHERE market IS NOT NULL)"
    ).fetchone()
    db.close()
    OUT.unlink(missing_ok=True)
    tmp.rename(OUT)
    print(f"\nKész: {cards} kártya, ebből {with_price} kártyának van piaci ára.")
    print(f"Fájl: {OUT} ({OUT.stat().st_size / 1e6:.1f} MB), dátum: {remote_date}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Megszakítva. A letöltött fájlok a tcgcsv_cache mappában maradtak.")
