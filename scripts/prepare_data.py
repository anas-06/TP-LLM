"""Télécharge Re-DocRED et produit les fichiers utilisés par l'application.

- data/fewshot_set.json : documents du split *dev*, réservés à la construction
  des prompts (exemples few-shot) ;
- data/eval_set.json    : documents du split *test*, réservés à l'évaluation.

Les deux ensembles sont disjoints : aucun exemple d'évaluation n'apparaît
dans un prompt.

Usage : python -m scripts.prepare_data [--n-eval 100] [--n-fewshot 100]
"""

from __future__ import annotations

import argparse
import json
import urllib.request

from backend.dataset import DATA_DIR, RAW_URL, convert_document


def load_raw(split: str) -> list[dict]:
    path = DATA_DIR / "raw" / f"{split}_revised.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = RAW_URL.format(split=split)
        print(f"Téléchargement {url}")
        urllib.request.urlretrieve(url, path)
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-eval", type=int, default=100)
    parser.add_argument("--n-fewshot", type=int, default=100)
    args = parser.parse_args()

    for split, name, n in [("test", "eval", args.n_eval), ("dev", "fewshot", args.n_fewshot)]:
        raw = load_raw(split)[:n]
        docs = [convert_document(d, f"{split}-{i}") for i, d in enumerate(raw)]
        out = DATA_DIR / f"{name}_set.json"
        out.write_text(json.dumps(docs, ensure_ascii=False, indent=1), encoding="utf-8")
        n_triples = sum(len(d["triples"]) for d in docs)
        print(f"{out.name}: {len(docs)} documents, {n_triples} triplets")


if __name__ == "__main__":
    main()
