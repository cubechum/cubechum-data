"""Eldönti, kell-e új adatcsomag: a tcgcsv last-updated.txt újabb-e, mint a
legutóbbi Release manifestje. Kimenet (GITHUB_OUTPUT): build=true|false.

Használat: python should_build.py <manifest_url>
Ha a manifest nem érhető el (első futás), építünk.
"""
import os
import sys
import json
import urllib.error
import urllib.request
from datetime import datetime

UA = "CubeChumData/1.0 (github actions)"


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode().strip()


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> None:
    remote = get("https://tcgcsv.com/last-updated.txt")
    print(f"tcgcsv: {remote}")
    build = True
    try:
        current = json.loads(get(sys.argv[1]))["data_date"]
        print(f"Release: {current}")
        build = parse(remote) > parse(current)
    except (urllib.error.URLError, KeyError, ValueError) as e:
        print(f"Nincs használható manifest ({e}), építünk.")
    print(f"build={build}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"build={'true' if build else 'false'}\n")


if __name__ == "__main__":
    main()
