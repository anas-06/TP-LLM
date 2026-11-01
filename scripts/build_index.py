"""Construit l'index hybride du corpus de jurisprudence.

    python -m scripts.build_index            # BM25 + embeddings (quelques minutes sur CPU)
    python -m scripts.build_index --no-dense # BM25 seul (quelques secondes)
"""

from __future__ import annotations

import argparse
import time

from backend.juris.index import INDEX_DIR, HybridIndex


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-dense", action="store_true")
    ap.add_argument("--batch-size", type=int, default=64)
    args = ap.parse_args()
    t = time.time()
    idx = HybridIndex()
    idx.build(with_dense=not args.no_dense, batch_size=args.batch_size)
    idx.save()
    print(f"{len(idx.chunks)} passages, {len(idx.vocab)} termes, dense={idx.has_dense} "
          f"-> {INDEX_DIR} ({time.time() - t:.0f} s)")


if __name__ == "__main__":
    main()
