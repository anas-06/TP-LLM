"""Préparation du corpus de jurisprudence (TP2/TP3).

Lit en continu le fichier `cour_de_cassation.jsonl.gz` du dataset Hugging Face
`antoinejeannot/jurisprudence` (environ 1 Go compressé, rien n'est stocké en entier)
et garde un sous-ensemble cohérent :
  - décisions de la Cour de cassation rendues depuis `--since` ;
  - publiées au Bulletin (décisions qui font jurisprudence, texte plus riche) ;
  - découpage en zones disponible (introduction, moyens, motivations, dispositif) ;
  - échantillon équilibré par chambre (tirage aléatoire reproductible).

Sortie : data/juris/cases.jsonl (une décision par ligne).

    python -m scripts.prepare_juris --n 2000 --since 2015-01-01
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

URL = ("https://huggingface.co/datasets/antoinejeannot/jurisprudence/resolve/main/"
       "cour_de_cassation.jsonl.gz")
OUT = Path(__file__).resolve().parent.parent / "data" / "juris" / "cases.jsonl"
KEEP = ["id", "number", "ecli", "decision_date", "chamber", "formation", "solution", "type",
        "themes", "summary", "publication", "text", "zones"]


def keep(d: dict, since: str) -> bool:
    if (d.get("decision_date") or "") < since:
        return False
    if not d.get("zones") or not d.get("text"):
        return False
    if not 2500 <= len(d["text"]) <= 60000:
        return False
    pub = " ".join(d.get("publication") or [])
    return "Bulletin" in pub or bool(d.get("particularInterest"))


def slim(d: dict) -> dict:
    out = {k: d.get(k) for k in KEEP}
    out["visa"] = [v.get("title") for v in (d.get("visa") or []) if v.get("title")]
    c = d.get("contested") or {}
    out["contested"] = c.get("title")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--since", default="2015-01-01")
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    per_chamber = max(50, args.n // 6)
    reservoirs: dict[str, list[dict]] = defaultdict(list)
    seen: dict[str, int] = defaultdict(int)

    t0 = time.time()
    with urllib.request.urlopen(URL, timeout=120) as resp, gzip.GzipFile(fileobj=resp) as g:
        for i, line in enumerate(g):
            if i % 50000 == 0:
                kept = sum(seen.values())
                print(f"  {i:>7} décisions lues, {kept} candidates ({time.time() - t0:.0f} s)",
                      file=sys.stderr, flush=True)
            d = json.loads(line)
            if not keep(d, args.since):
                continue
            ch = d.get("chamber") or "Autre"
            seen[ch] += 1
            res = reservoirs[ch]
            if len(res) < per_chamber:  # tirage par réservoir, mémoire bornée
                res.append(slim(d))
            else:
                j = rng.randrange(seen[ch])
                if j < per_chamber:
                    res[j] = slim(d)

    cases = [c for res in reservoirs.values() for c in res]
    rng.shuffle(cases)
    cases = sorted(cases[: args.n], key=lambda c: c["decision_date"], reverse=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"{len(cases)} décisions écrites dans {OUT}")
    for ch, n in sorted(seen.items(), key=lambda x: -x[1]):
        print(f"  {ch}: {n} candidates, {len(reservoirs[ch])} gardées")


if __name__ == "__main__":
    main()
