"""Chargement et conversion du jeu de données Re-DocRED.

Format interne d'un document :
{
  "id": "test-12", "title": "...",
  "sents": ["phrase 1", "phrase 2", ...],          # texte détokénisé
  "entities": [{"names": ["Alice Martin", "Martin"], "type": "PER", "sent_ids": [0, 2]}],
  "triples": [{"subject_id": 0, "object_id": 3, "code": "P69",
               "relation": "educated_at", "subject": "Alice Martin",
               "object": "Stanford University", "evidence": [0]}]
}
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .relations import RELATIONS

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_URL = "https://raw.githubusercontent.com/tonytan48/Re-DocRED/main/data/{split}_revised.json"


def detokenize(tokens: list[str]) -> str:
    text = " ".join(tokens)
    text = re.sub(r" ([,.;:!?%)\]}])", r"\1", text)
    text = re.sub(r"([(\[{$]) ", r"\1", text)
    text = re.sub(r" (n't|'s|'re|'ve|'ll|'d|'m)\b", r"\1", text)
    text = re.sub(r"(\w)s ' ", r"\1s' ", text)
    text = re.sub(r"`` ?| ?''", '"', text)
    text = re.sub(r" - ", "-", text)
    return text


def convert_document(raw: dict, doc_id: str) -> dict:
    sents = [detokenize(s) for s in raw["sents"]]
    entities = []
    for mentions in raw["vertexSet"]:
        names: list[str] = []
        for m in mentions:
            name = detokenize(m["name"].split())
            if name not in names:
                names.append(name)
        entities.append({
            "names": names,
            "type": mentions[0]["type"],
            "sent_ids": sorted({m["sent_id"] for m in mentions}),
        })
    triples = []
    for lab in raw["labels"]:
        if lab["r"] not in RELATIONS:
            continue
        triples.append({
            "subject_id": lab["h"],
            "object_id": lab["t"],
            "code": lab["r"],
            "relation": RELATIONS[lab["r"]][0],
            "subject": entities[lab["h"]]["names"][0],
            "object": entities[lab["t"]]["names"][0],
            "evidence": sorted(lab.get("evidence", [])),
        })
    return {"id": doc_id, "title": raw["title"], "sents": sents,
            "entities": entities, "triples": triples}


def doc_text(doc: dict) -> str:
    return " ".join(doc["sents"])


def truncate(doc: dict, max_sents: int | None) -> dict:
    """Garde les `max_sents` premières phrases et les triplets encore supportés."""
    if not max_sents or max_sents >= len(doc["sents"]):
        return doc
    keep_ent = {i for i, e in enumerate(doc["entities"]) if any(s < max_sents for s in e["sent_ids"])}
    triples = [
        t for t in doc["triples"]
        if t["subject_id"] in keep_ent and t["object_id"] in keep_ent
        and all(s < max_sents for s in t["evidence"])
    ]
    return {**doc, "sents": doc["sents"][:max_sents], "triples": triples}


def filter_relations(doc: dict, codes: list[str]) -> dict:
    allowed = set(codes)
    return {**doc, "triples": [t for t in doc["triples"] if t["code"] in allowed]}


@lru_cache(maxsize=4)
def load_split(name: str) -> list[dict]:
    """name = 'eval' (issu du test Re-DocRED) ou 'fewshot' (issu du dev)."""
    path = DATA_DIR / f"{name}_set.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} introuvable : lancez d'abord `python -m scripts.prepare_data`."
        )
    return json.loads(path.read_text(encoding="utf-8"))
