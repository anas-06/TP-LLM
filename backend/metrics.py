"""Métriques d'évaluation par rapport aux annotations Re-DocRED.

Une relation prédite (s, r, o) est correcte s'il existe un triplet de référence
(h, r, t) tel que s correspond à l'une des mentions de h et o à l'une des
mentions de t (comparaison normalisée : casse, ponctuation, article initial).
"""

from __future__ import annotations

from .validation import normalize, strip_article


def _forms(names: list[str]) -> set[str]:
    out = set()
    for n in names:
        nn = normalize(n)
        out |= {nn, strip_article(nn)}
    return out


def gold_matcher(doc: dict):
    """Renvoie une fonction rel -> ensemble des triplets de référence correspondants."""
    ent_forms = [_forms(e["names"]) for e in doc["entities"]]
    gold = {(t["subject_id"], t["relation"], t["object_id"]) for t in doc["triples"]}

    def match(rel: dict) -> set[tuple]:
        s, o = normalize(rel["subject"]), normalize(rel["object"])
        s_ids = {i for i, f in enumerate(ent_forms) if s in f or strip_article(s) in f}
        o_ids = {i for i, f in enumerate(ent_forms) if o in f or strip_article(o) in f}
        return {(h, rel["relation"], t) for h in s_ids for t in o_ids} & gold

    return match, gold


def mark_correct(rels: list[dict], doc: dict) -> None:
    match, _ = gold_matcher(doc)
    for rel in rels:
        rel["correct"] = bool(match(rel))


def score_document(pred: list[dict], raw_pred: list[dict], doc: dict) -> dict:
    """pred : relations retenues après validation ;
    raw_pred : toutes les relations analysées (pour mesurer les hallucinations)."""
    match, gold = gold_matcher(doc)
    matched_gold: set[tuple] = set()
    tp_pred = 0
    for rel in pred:
        m = match(rel)
        if m:
            tp_pred += 1
            matched_gold |= m
    return {
        "n_pred": len(pred),
        "n_gold": len(gold),
        "tp_pred": tp_pred,
        "tp_gold": len(matched_gold),
        "n_raw": len(raw_pred),
        "n_ungrounded": sum(not r["grounded"] for r in raw_pred),
        "n_unknown_rel": sum(not r["known_relation"] for r in raw_pred),
    }


def aggregate(rows: list[dict]) -> dict:
    """Agrège les scores de plusieurs documents (micro-moyenne)."""
    n = len(rows) or 1
    s = {k: sum(r[k] for r in rows) for k in
         ("n_pred", "n_gold", "tp_pred", "tp_gold", "n_raw", "n_ungrounded", "n_unknown_rel")}
    p = s["tp_pred"] / s["n_pred"] if s["n_pred"] else 0.0
    r = s["tp_gold"] / s["n_gold"] if s["n_gold"] else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {
        "precision": p,
        "recall": r,
        "f1": f1,
        "json_valid_rate": sum(x["json_valid"] for x in rows) / n,
        "schema_valid_rate": sum(x["schema_valid"] for x in rows) / n,
        "hallucination_rate": s["n_ungrounded"] / s["n_raw"] if s["n_raw"] else 0.0,
        "off_schema_rate": s["n_unknown_rel"] / s["n_raw"] if s["n_raw"] else 0.0,
        "avg_pred": s["n_pred"] / n,
        "avg_latency_s": sum(x["latency_s"] for x in rows) / n,
        **s,
    }
