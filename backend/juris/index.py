"""Index hybride : BM25 (lexical) + embeddings multilingues (dense), fusion par RRF.

- BM25 : mots exacts du droit (« article 1240 », « salarié protégé », numéros).
  Tokenisation française : minuscules, accents retirés, mots vides, racinisation Snowball.
- Dense : `intfloat/multilingual-e5-small` (118 M paramètres, tourne sur CPU), utile
  pour les questions en langage naturel qui n'emploient pas les termes de la décision.
- Fusion : Reciprocal Rank Fusion (k = 60), insensible aux échelles de score.

Construction : python -m scripts.build_index   (fichiers dans data/juris/index/)
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np
from scipy import sparse

from .corpus import DATA_DIR, chunk_case, load_cases

INDEX_DIR = DATA_DIR / "index"
EMBED_MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small")
K1, B, RRF_K = 1.5, 0.75, 60

_STOP = set("""
a au aux avec ce ces cet cette dans de des du elle en et eux il ils je la le les leur leurs lui
ma mais me meme mes moi mon ne nos notre nous on ou par pas pour qu que qui sa se ses son sur ta te
tes toi ton tu un une vos votre vous c d j l m n s t y ete etre est sont a ont avait etait sera
cela ceci celui celle ceux celles dont ainsi alors donc or ni car si plus moins tres sans sous
entre vers chez lors apres avant depuis pendant comme tout tous toute toutes autre autres meme
quel quelle quels quelles quoi comment pourquoi quand elle cour arret attendu
""".split())

try:
    from nltk.stem.snowball import FrenchStemmer
    _stem = FrenchStemmer().stem
except Exception:  # repli : suppression des pluriels
    def _stem(w: str) -> str:
        return w[:-1] if len(w) > 4 and w[-1] in "sx" else w


def strip_accents(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def tokenize(text: str) -> list[str]:
    text = strip_accents(text.lower())
    toks = re.findall(r"[a-z0-9]+(?:[.-][0-9]+)*", text)
    return [_stem(t) if not t[0].isdigit() else t for t in toks if t not in _STOP and len(t) > 1]


class HybridIndex:
    def __init__(self) -> None:
        self.chunks: list[dict] = []
        self.vocab: dict[str, int] = {}
        self.idf: np.ndarray | None = None
        self.W: sparse.csr_matrix | None = None  # poids BM25 (passages x termes)
        self.emb: np.ndarray | None = None
        self.case_rows: dict[str, list[int]] = {}
        self._encoder = None
        self._lock = threading.Lock()

    # ---------- construction ----------
    def build(self, with_dense: bool = True, batch_size: int = 64) -> None:
        cases = load_cases()
        self.chunks = [ch for c in cases.values() for ch in chunk_case(c)]
        docs = [tokenize(ch["text"]) for ch in self.chunks]
        df: Counter = Counter()
        for d in docs:
            df.update(set(d))
        self.vocab = {t: i for i, t in enumerate(sorted(df))}
        n = len(docs)
        self.idf = np.array([math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in sorted(df)],
                            dtype=np.float32)
        avgdl = sum(map(len, docs)) / n
        rows, cols, vals = [], [], []
        for r, d in enumerate(docs):
            dl = len(d)
            for t, tf in Counter(d).items():
                j = self.vocab[t]
                rows.append(r)
                cols.append(j)
                vals.append(self.idf[j] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * dl / avgdl)))
        self.W = sparse.csr_matrix((vals, (rows, cols)), shape=(n, len(self.vocab)), dtype=np.float32)
        if with_dense:
            texts = ["passage: " + ch["text"] for ch in self.chunks]
            self.emb = self.encoder().encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                             show_progress_bar=True, convert_to_numpy=True).astype(np.float16)
        self._index_rows()

    def save(self) -> None:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        with (INDEX_DIR / "chunks.jsonl").open("w", encoding="utf-8") as f:
            for ch in self.chunks:
                f.write(json.dumps(ch, ensure_ascii=False) + "\n")
        (INDEX_DIR / "vocab.json").write_text(json.dumps(self.vocab), encoding="utf-8")
        np.save(INDEX_DIR / "idf.npy", self.idf)
        sparse.save_npz(INDEX_DIR / "bm25.npz", self.W)
        if self.emb is not None:
            np.save(INDEX_DIR / "emb.npy", self.emb)
        (INDEX_DIR / "meta.json").write_text(json.dumps({
            "n_chunks": len(self.chunks), "n_terms": len(self.vocab), "embed_model": EMBED_MODEL,
            "dense": self.emb is not None, "k1": K1, "b": B}), encoding="utf-8")

    @classmethod
    def load(cls) -> "HybridIndex":
        if not (INDEX_DIR / "bm25.npz").exists():
            raise FileNotFoundError("Index absent : lancez `python -m scripts.build_index`.")
        idx = cls()
        with (INDEX_DIR / "chunks.jsonl").open(encoding="utf-8") as f:
            idx.chunks = [json.loads(line) for line in f]
        idx.vocab = json.loads((INDEX_DIR / "vocab.json").read_text(encoding="utf-8"))
        idx.idf = np.load(INDEX_DIR / "idf.npy")
        idx.W = sparse.load_npz(INDEX_DIR / "bm25.npz").tocsr()
        if (INDEX_DIR / "emb.npy").exists():
            idx.emb = np.load(INDEX_DIR / "emb.npy").astype(np.float32)
        idx._index_rows()
        return idx

    def _index_rows(self) -> None:
        self.case_rows = {}
        for i, ch in enumerate(self.chunks):
            self.case_rows.setdefault(ch["case_id"], []).append(i)

    def encoder(self):
        with self._lock:
            if self._encoder is None:
                from sentence_transformers import SentenceTransformer
                self._encoder = SentenceTransformer(EMBED_MODEL, device="cpu")
                # Passages d'environ 170 mots : 192 tokens couvrent l'essentiel, 2x plus rapide que 512.
                self._encoder.max_seq_length = int(os.getenv("EMBED_MAX_TOKENS", "192"))
        return self._encoder

    @property
    def has_dense(self) -> bool:
        return self.emb is not None

    # ---------- recherche ----------
    def bm25_scores(self, query: str, rows: np.ndarray | None = None) -> np.ndarray:
        q = np.zeros(len(self.vocab), dtype=np.float32)
        for t in set(tokenize(query)):
            j = self.vocab.get(t)
            if j is not None:
                q[j] = 1.0
        W = self.W if rows is None else self.W[rows]
        return np.asarray(W @ q).ravel()

    def dense_scores(self, query: str, rows: np.ndarray | None = None) -> np.ndarray:
        qv = self.encoder().encode(["query: " + query], normalize_embeddings=True)[0].astype(np.float32)
        E = self.emb if rows is None else self.emb[rows]
        return E @ qv

    def search_chunks(self, query: str, k: int = 50, mode: str = "hybrid",
                      rows: list[int] | None = None) -> list[tuple[int, float, dict]]:
        """Renvoie [(indice du passage, score, détail)] triés par pertinence décroissante."""
        r = None if rows is None else np.asarray(rows)
        mode = mode if (mode != "hybrid" or self.has_dense) else "bm25"
        if mode == "dense" and not self.has_dense:
            mode = "bm25"
        ranks: dict[str, dict[int, int]] = {}
        raw: dict[str, np.ndarray] = {}
        for m in (["bm25", "dense"] if mode == "hybrid" else [mode]):
            s = self.bm25_scores(query, r) if m == "bm25" else self.dense_scores(query, r)
            raw[m] = s
            depth = min(len(s), max(k * 4, 200))
            top = np.argpartition(-s, depth - 1)[:depth] if depth < len(s) else np.arange(len(s))
            top = top[np.argsort(-s[top])]
            if m == "bm25":
                top = top[s[top] > 0]
            ranks[m] = {int(i): rank for rank, i in enumerate(top)}
        fused: dict[int, float] = {}
        for m, rk in ranks.items():
            for i, rank in rk.items():
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
        best = sorted(fused.items(), key=lambda x: -x[1])[:k]
        out = []
        for i, score in best:
            gi = int(r[i]) if r is not None else i
            detail = {m: round(float(raw[m][i]), 4) for m in raw}
            out.append((gi, score, detail))
        return out


_INDEX: HybridIndex | None = None
_INDEX_LOCK = threading.Lock()


def get_index() -> HybridIndex:
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = HybridIndex.load()
    return _INDEX
