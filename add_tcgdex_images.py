"""TCGdex kártyaképek: cards.image_url -> assets.tcgdex.net (a TCGplayer helyett).

A cím a TCGdex adattárból áll össze (MIT):
  https://assets.tcgdex.net/en/{sorozat}/{szett}/{szám}/low.webp
A párosítás a TCGplayer productId-n megy (ugyanúgy, mint az EUR áraknál).

Hogy ne érjük el a CDN-t egyszerre sok kéréssel, minden futás legfeljebb --limit
(alap: 3000) képet ellenőriz egy HEAD kéréssel, és az eredményt az --state
fájlba (image_check.json) írja: productId -> [1 = van, 0 = 404, ellenőrzés napja].
A következő futás onnan folytatja. A hiányzó (404) képet 14 nap múlva újra nézi.

Az adatbázisban:
  - van kép        -> image_url = a TCGdex 'low.webp' cím
  - 404 vagy nincs TCGdex-párja (már ellenőrzött) -> image_url = NULL (az app ikont mutat)
  - még nem ellenőrzött -> változatlan (egyelőre a régi cím marad)

Hibára (429/403) azonnal leáll az ellenőrzéssel, a addig megszerzett állapotot menti.
"""
import argparse
import json
import re
import sqlite3
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

TCGDEX_TARBALL = "https://codeload.github.com/tcgdex/cards-database/tar.gz/refs/heads/master"
ASSETS = "https://assets.tcgdex.net/en"
UA = "CubeCubby-data/0.1 (+https://github.com/cubechum/cubechum-data)"
PAUSE = 0.3         # másodperc két kérés között
RETRY_WAITS = (2, 5, 10)  # másodperc: ennyit várunk 502/503/504 vagy hálózati hiba után
RECHECK_DAYS = 14    # a 404-es képet ennyi nap után nézzük újra
MIN_URLS = 15000      # védelem: csonka tarball ne törölje a képeket

ID_RE = re.compile(r'^\s*id:\s*"([^"]+)"', re.M)
BLOCK_RE = re.compile(r"thirdParty:\s*\{([^}]*)\}", re.S)
TP_RE = re.compile(r"tcgplayer:\s*(\d+)")


def fetch(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=180)


def load_urls() -> dict[int, str]:
    """TCGplayer productId -> TCGdex képcím (low.webp)."""
    files: dict[str, str] = {}
    with fetch(TCGDEX_TARBALL) as r, tarfile.open(fileobj=r, mode="r|gz") as tar:
        for m in tar:
            if not m.isfile() or not m.name.endswith(".ts") or "/data/" not in m.name:
                continue
            rel = m.name.split("/data/", 1)[1][:-3]
            files[rel] = tar.extractfile(m).read().decode("utf-8", "ignore")

    serie_id: dict[str, str] = {}
    set_id: dict[tuple[str, str], str] = {}
    for rel, text in files.items():
        parts = rel.split("/")
        m = ID_RE.search(text)
        if not m:
            continue
        if len(parts) == 1:
            serie_id[parts[0]] = m.group(1)
        elif len(parts) == 2:
            set_id[(parts[0], parts[1])] = m.group(1)

    urls: dict[int, str] = {}
    for rel, text in files.items():
        parts = rel.split("/")
        if len(parts) != 3:
            continue
        serie, setname, local = parts
        pid = None
        for block in BLOCK_RE.findall(text):
            m = TP_RE.search(block)
            if m:
                pid = int(m.group(1))
        sid, zid = serie_id.get(serie), set_id.get((serie, setname))
        if pid is None or not sid or not zid:
            continue
        urls[pid] = f"{ASSETS}/{sid}/{zid}/{local}/low.webp"
    if len(urls) < MIN_URLS:
        raise SystemExit(f"Gyanúsan kevés TCGdex képcím: {len(urls)}")
    return urls


def load_state(path: str) -> dict[str, list]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(path: str, state: dict[str, list]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"), sort_keys=True)


def due(entry: list | None, cutoff: str) -> bool:
    """Kell-e (újra) ellenőrizni."""
    if entry is None:
        return True
    return entry[0] == 0 and entry[1] <= cutoff


def check(url: str) -> int:
    """1 = van, 0 = 404. Más hiba: kivétel."""
    for wait in (*RETRY_WAITS, None):
        try:
            with fetch(url, "HEAD") as r:
                return 1 if r.status == 200 else 0
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return 0
            # A 502/503/504 szakaszos szerveroldali hiba: várunk és újra próbáljuk.
            if e.code not in (502, 503, 504) or wait is None:
                raise
        except OSError:
            if wait is None:
                raise
        time.sleep(wait)
    raise AssertionError("elérhetetlen")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="cards.sqlite")
    ap.add_argument("--state", default="image_check.json")
    ap.add_argument("--limit", type=int, default=3000)
    args = ap.parse_args()

    urls = load_urls()
    state = load_state(args.state)
    db = sqlite3.connect(args.db)
    products = [r[0] for r in db.execute("SELECT product_id FROM cards ORDER BY product_id")]
    print(f"TCGdex képcím: {len(urls)} | kártya: {len(products)} | állapotban: {len(state)}")

    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=RECHECK_DAYS)).isoformat()

    # Ellenőrizendő: azoknak, akiknek van TCGdex címük. Akinek nincs, azt nem kell
    # kérni, az azonnal "nincs kép".
    todo = [p for p in products if p in urls and due(state.get(str(p)), cutoff)]
    print(f"Ellenőrizendő: {len(todo)} | ebben a futásban: {min(len(todo), args.limit)}")

    done = ok = missing = 0
    stopped = ""
    errors_in_row = 0
    try:
        for pid in todo[: args.limit]:
            try:
                res = check(urls[pid])
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    stopped = f"HTTP {e.code}: leállok"
                    break
                errors_in_row += 1
                print(f"  hiba: HTTP {e.code} ({urls[pid]})")
            except OSError as e:
                errors_in_row += 1
                print(f"  hiba: {type(e).__name__}: {e} ({urls[pid]})")
            else:
                errors_in_row = 0
                state[str(pid)] = [res, today]
                done += 1
                ok += res
                missing += 1 - res
            if errors_in_row >= 5:
                stopped = "5 hiba egymás után: leállok"
                break
            time.sleep(PAUSE)
    finally:
        save_state(args.state, state)  # a félkész eredmény sem vész el

    print(f"Ellenőrizve: {done} (van: {ok}, 404: {missing})" + (f" | {stopped}" if stopped else ""))

    # Az adatbázis frissítése. Akinek nincs TCGdex-címe, az "nincs kép".
    changed_ok = changed_none = pending = 0
    with db:
        for pid in products:
            if pid not in urls:
                db.execute("UPDATE cards SET image_url = NULL WHERE product_id = ?", (pid,))
                changed_none += 1
                continue
            entry = state.get(str(pid))
            if entry is None:
                pending += 1
            elif entry[0] == 1:
                db.execute("UPDATE cards SET image_url = ? WHERE product_id = ?", (urls[pid], pid))
                changed_ok += 1
            else:
                db.execute("UPDATE cards SET image_url = NULL WHERE product_id = ?", (pid,))
                changed_none += 1
        db.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            [
                ("images_source", "TCGdex assets (assets.tcgdex.net)"),
                ("images_ok", str(changed_ok)),
                ("images_none", str(changed_none)),
                ("images_pending", str(pending)),
            ],
        )
    db.close()
    print(
        f"Adatbázis: TCGdex kép {changed_ok} | nincs kép {changed_none} | "
        f"még nem ellenőrzött (régi cím marad) {pending}"
    )
    if stopped:
        sys.exit(3)  # a hívó (kézi ciklus) tudja, hogy meg kell állnia


if __name__ == "__main__":
    main()
