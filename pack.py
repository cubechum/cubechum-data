"""cards.sqlite -> cards.sqlite.gz + manifest.json

A manifest: { "data_date", "schema", "size" (a tÃ¶mÃ¶rÃ­tett fÃ¡jlÃ©), "sha256" (a tÃ¶mÃ¶rÃ­tettÃ©),
"uncompressed_size" }. A data_date a cards.sqlite meta tÃ¡blÃ¡jÃ¡bÃ³l jÃ¶n.
"""
import gzip
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

SRC = Path("cards.sqlite")
GZ = Path("cards.sqlite.gz")


def main() -> None:
    db = sqlite3.connect(SRC)
    (data_date,) = db.execute("SELECT value FROM meta WHERE key='data_date'").fetchone()
    (cards,) = db.execute("SELECT COUNT(*) FROM cards").fetchone()
    row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    db.close()
    if row is None:
        raise SystemExit("A meta táblában nincs schema: nem tudjuk, milyen szerkezetű")
    schema = row[0]
    if cards < 20000:  # védelem: egy csonka build ne írja felül a jó csomagot
        raise SystemExit(f"Gyanúsan kevés kártya: {cards}")

    with SRC.open("rb") as f, gzip.GzipFile(GZ, "wb", compresslevel=9, mtime=0) as g:
        shutil.copyfileobj(f, g)

    manifest = {
        "data_date": data_date,
        # Az adatbázis szerkezeti verziója: a régi appok ebből tudják, hogy az
        # újabb szerkezetű csomagot nem szabad letölteniük.
        "schema": schema,
        "size": GZ.stat().st_size,
        "sha256": hashlib.sha256(GZ.read_bytes()).hexdigest(),
        "uncompressed_size": SRC.stat().st_size,
    }
    Path("manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(manifest)


if __name__ == "__main__":
    main()
