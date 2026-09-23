"""Construction des prompts pour chaque stratégie.

Stratégies (cumulatives, pour isoler l'effet de chaque ajout) :
- zero_shot : consigne minimale + format JSON attendu ;
- context   : + rôle, liste des relations autorisées avec définitions, règles ;
- few_shot  : context + k exemples tirés du split dev (jamais du split d'évaluation) ;
- cot       : few_shot + raisonnement explicite (champ "reasoning") avant la réponse ;
- decompose : deux appels : (1) extraction des entités, (2) relations entre ces entités.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from .dataset import doc_text, filter_relations, load_split, truncate
from .relations import RELATIONS
from .validation import is_grounded, normalize

STRATEGIES = {
    "zero_shot": "Zero-shot minimal : consigne courte et format JSON, sans schéma de relations.",
    "context": "Zero-shot + contexte : rôle, relations autorisées avec définitions, règles anti-hallucination.",
    "few_shot": "Contexte + exemples (few-shot) issus du split dev de Re-DocRED.",
    "cot": "Few-shot + raisonnement : le modèle liste entités et preuves dans \"reasoning\" avant les relations.",
    "decompose": "Décomposition en 2 appels : extraction des entités, puis des relations entre ces entités.",
}

JSON_FORMAT = '{"relations": [{"subject": "...", "relation": "...", "object": "..."}]}'
COT_FORMAT = '{"reasoning": "...", "relations": [{"subject": "...", "relation": "...", "object": "..."}]}'


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text.strip())
    return [p for p in parts if p]


def numbered(sents: list[str]) -> str:
    return "\n".join(f"[{i + 1}] {s}" for i, s in enumerate(sents))


def relation_block(codes: list[str]) -> str:
    return "\n".join(f"- {RELATIONS[c][0]}: {RELATIONS[c][1]}" for c in codes)


def system_prompt(codes: list[str], cot: bool = False) -> str:
    fmt = COT_FORMAT if cot else JSON_FORMAT
    steps = ""
    if cot:
        steps = (
            "\nBefore answering, think step by step inside the \"reasoning\" field:\n"
            "1. list the named entities (people, organizations, places, dates, works);\n"
            "2. for each candidate fact, cite the sentence number that states it;\n"
            "3. keep only the facts explicitly supported by a sentence.\n"
            "Keep the reasoning short (max 8 lines).\n"
        )
    return (
        "You are an information extraction system. You read an English text and "
        "extract the factual relations between the entities it mentions.\n\n"
        "Allowed relations (use exactly these names, subject first):\n"
        f"{relation_block(codes)}\n\n"
        "Rules:\n"
        "- Only extract a relation if the text explicitly supports it. Never use outside knowledge.\n"
        "- Copy entity names exactly as they appear in the text (no pronouns, no paraphrase).\n"
        "- Respect the direction of each relation as defined above.\n"
        "- Do not use any relation name outside the list. If nothing applies, return an empty list.\n"
        f"{steps}\n"
        f"Answer only with JSON in this format:\n{fmt}"
    )


# --------------------------------------------------------------------------- few-shot

def _name_in_text(entity: dict, text: str) -> str | None:
    tn = normalize(text)
    for name in entity["names"]:
        if is_grounded(name, tn):
            return name
    return None


def _demo_from_doc(doc: dict, codes: list[str], max_sents: int, max_triples: int) -> dict | None:
    d = filter_relations(truncate(doc, max_sents), codes)
    text = doc_text(d)
    triples, seen = [], set()
    for t in d["triples"]:
        s = _name_in_text(d["entities"][t["subject_id"]], text)
        o = _name_in_text(d["entities"][t["object_id"]], text)
        if not s or not o or (s, t["relation"], o) in seen:
            continue
        seen.add((s, t["relation"], o))
        ev = [e + 1 for e in t["evidence"]] or [min(d["entities"][t["subject_id"]]["sent_ids"]) + 1]
        triples.append({"subject": s, "relation": t["relation"], "object": o, "evidence": ev})
    if len(triples) < 3:
        return None
    triples = triples[:max_triples]
    entities = []
    for e in d["entities"]:
        name = _name_in_text(e, text)
        if name and name not in [x["name"] for x in entities]:
            entities.append({"name": name, "type": e["type"]})
    return {"id": doc["id"], "sents": d["sents"], "text": text, "triples": triples, "entities": entities}


# Exemples relus à la main dans le split dev : annotations propres et domaines
# variés (personne/politique, musique, organisation, études, sport, famille).
# La sélection automatique pure retenait des documents aux annotations bruitées.
CURATED_DEMOS = ["dev-15", "dev-47", "dev-24", "dev-1", "dev-26", "dev-18"]


@lru_cache(maxsize=16)
def _select_demos(codes: tuple[str, ...], k: int, max_sents: int, max_triples: int) -> list[dict]:
    """Choix déterministe : exemples relus d'abord, puis glouton sur la couverture."""
    candidates = []
    for doc in load_split("fewshot"):
        demo = _demo_from_doc(doc, list(codes), max_sents, max_triples)
        if demo:
            candidates.append(demo)
    by_id = {c["id"]: c for c in candidates}
    chosen = [by_id[i] for i in CURATED_DEMOS if i in by_id][:k]
    covered = {t["relation"] for c in chosen for t in c["triples"]}
    for _ in range(min(k, len(candidates)) - len(chosen)):
        best = max(
            (c for c in candidates if c not in chosen),
            key=lambda c: (len({t["relation"] for t in c["triples"]} - covered),
                           len(c["triples"]), -len(c["text"])),
        )
        chosen.append(best)
        covered |= {t["relation"] for t in best["triples"]}
    return chosen


def select_demos(codes: list[str], k: int = 3, max_sents: int = 3, max_triples: int = 8) -> list[dict]:
    if k <= 0:
        return []
    return _select_demos(tuple(codes), k, max_sents, max_triples)


def _answer(demo: dict, cot: bool) -> str:
    rels = [{k: t[k] for k in ("subject", "relation", "object")} for t in demo["triples"]]
    if not cot:
        return json.dumps({"relations": rels}, ensure_ascii=False)
    ents = ", ".join(e["name"] for e in demo["entities"][:12])
    lines = [f"Entities: {ents}."]
    for t in demo["triples"]:
        src = ", ".join(str(e) for e in t["evidence"])
        lines.append(f"Sentence {src}: {t['subject']} -> {t['relation']} -> {t['object']}.")
    return json.dumps({"reasoning": "\n".join(lines), "relations": rels}, ensure_ascii=False)


# --------------------------------------------------------------------------- builders

def user_message(text: str, numbered_sents: bool = False) -> str:
    body = numbered(split_sentences(text)) if numbered_sents else text
    return f"Text:\n{body}"


def build_messages(strategy: str, text: str, codes: list[str], n_shots: int = 3) -> list[dict]:
    """Messages pour les stratégies à un seul appel."""
    if strategy == "zero_shot":
        return [{"role": "user", "content": (
            "Extract the entities and the factual relations between them from the text below.\n"
            f"Answer only with JSON in this format: {JSON_FORMAT}\n\n{user_message(text)}"
        )}]
    cot = strategy == "cot"
    messages = [{"role": "system", "content": system_prompt(codes, cot=cot)}]
    if strategy in ("few_shot", "cot"):
        for demo in select_demos(codes, n_shots):
            src = numbered(demo["sents"]) if cot else demo["text"]
            messages.append({"role": "user", "content": f"Text:\n{src}"})
            messages.append({"role": "assistant", "content": _answer(demo, cot)})
    elif strategy != "context":
        raise ValueError(f"stratégie inconnue : {strategy}")
    messages.append({"role": "user", "content": user_message(text, numbered_sents=cot)})
    return messages


ENTITY_SYSTEM = (
    "You are a named entity recognizer. List every named entity of the text: people (PER), "
    "organizations (ORG), locations (LOC), dates and years (TIME), numbers (NUM) and other "
    "names such as works, events or nationalities (MISC). Copy each name exactly as written "
    "in the text and list each entity only once.\n"
    'Answer only with JSON: {"entities": [{"name": "...", "type": "PER"}]}'
)


def build_entity_messages(text: str, codes: list[str], n_shots: int = 3) -> list[dict]:
    messages = [{"role": "system", "content": ENTITY_SYSTEM}]
    for demo in select_demos(codes, n_shots):
        messages.append({"role": "user", "content": f"Text:\n{demo['text']}"})
        messages.append({"role": "assistant",
                         "content": json.dumps({"entities": demo["entities"]}, ensure_ascii=False)})
    messages.append({"role": "user", "content": user_message(text)})
    return messages


def build_relation_messages(text: str, entities: list[dict], codes: list[str],
                            n_shots: int = 3) -> list[dict]:
    def fmt(ents: list[dict]) -> str:
        return "\n".join(f"- {e['name']} ({e.get('type', '?')})" for e in ents)

    system = system_prompt(codes) + (
        "\n\nYou are also given the list of entities found in the text. "
        "Subjects and objects must be taken from this list."
    )
    messages = [{"role": "system", "content": system}]
    for demo in select_demos(codes, n_shots):
        messages.append({"role": "user",
                         "content": f"Text:\n{demo['text']}\n\nEntities:\n{fmt(demo['entities'])}"})
        messages.append({"role": "assistant", "content": _answer(demo, cot=False)})
    messages.append({"role": "user", "content": f"{user_message(text)}\n\nEntities:\n{fmt(entities)}"})
    return messages
