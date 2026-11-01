"""Chargement des décisions et découpage en passages.

Chaque décision du dataset fournit des « zones » (positions dans le texte) :
introduction, exposé du litige, moyens, motivations, dispositif, annexes.
Le découpage suit ces zones, pour que chaque passage garde son rôle juridique
(un passage du dispositif n'a pas la même valeur qu'un moyen du pourvoi).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "juris"
CASES_PATH = DATA_DIR / "cases.jsonl"

SECTIONS = {
    "sommaire": "Sommaire",
    "introduction": "Introduction",
    "expose": "Exposé du litige",
    "moyens": "Moyens du pourvoi",
    "motivations": "Motivations de la Cour",
    "dispositif": "Dispositif",
    "annexes": "Annexes",
}
# Ordre de lecture d'une décision ; les annexes (moyens recopiés) ne sont pas indexées.
INDEXED_SECTIONS = ["introduction", "expose", "moyens", "motivations", "dispositif"]

CHAMBER_SHORT = {
    "Première chambre civile": "Civ. 1re",
    "Deuxième chambre civile": "Civ. 2e",
    "Troisième chambre civile": "Civ. 3e",
    "Chambre commerciale financière et économique": "Com.",
    "Chambre sociale": "Soc.",
    "Chambre criminelle": "Crim.",
    "Chambre mixte": "Ch. mixte",
    "Assemblée plénière": "Ass. plén.",
}

CHUNK_WORDS = 170
OVERLAP_WORDS = 35


@lru_cache(maxsize=1)
def load_cases() -> dict[str, dict]:
    if not CASES_PATH.exists():
        raise FileNotFoundError(
            f"{CASES_PATH} introuvable : lancez `python -m scripts.prepare_juris`.")
    cases = {}
    with CASES_PATH.open(encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            cases[c["id"]] = c
    return cases


def find_case(ref: str) -> dict | None:
    """Retrouve une décision par identifiant interne ou numéro de pourvoi (ex. 23-20.161)."""
    cases = load_cases()
    ref = (ref or "").strip()
    if ref in cases:
        return cases[ref]
    norm = re.sub(r"[^0-9]", "", ref)
    if len(norm) >= 7:
        for c in cases.values():
            if re.sub(r"[^0-9]", "", c.get("number") or "") == norm:
                return c
    return None


def clean(text: str) -> str:
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def case_title(c: dict) -> str:
    ch = CHAMBER_SHORT.get(c.get("chamber") or "", c.get("chamber") or "Cour de cassation")
    return f"Cass. {ch}, {c.get('decision_date', '?')}, n° {c.get('number') or '?'}"


def case_meta(c: dict) -> dict:
    return {
        "case_id": c["id"],
        "title": case_title(c),
        "number": c.get("number"),
        "date": c.get("decision_date"),
        "chamber": c.get("chamber"),
        "chamber_short": CHAMBER_SHORT.get(c.get("chamber") or "", c.get("chamber")),
        "solution": c.get("solution"),
        "formation": c.get("formation"),
        "ecli": c.get("ecli"),
        "themes": (c.get("themes") or [])[:8],
        "summary": c.get("summary"),
        "contested": c.get("contested"),
        "visa": c.get("visa") or [],
        "publication": c.get("publication") or [],
    }


def case_sections(c: dict) -> list[dict]:
    """Zones de la décision dans l'ordre du texte : [{section, label, text}]."""
    segs = []
    for name, spans in (c.get("zones") or {}).items():
        for s in spans or []:
            segs.append((s["start"], s["end"], name))
    segs.sort()
    out: list[dict] = []
    for start, end, name in segs:
        txt = clean(c["text"][start:end])
        if not txt:
            continue
        if out and out[-1]["section"] == name:  # zones contiguës de même type
            out[-1]["text"] += "\n" + txt
        else:
            out.append({"section": name, "label": SECTIONS.get(name, name), "text": txt})
    return out


def _windows(words: list[str]) -> list[str]:
    if len(words) <= CHUNK_WORDS + OVERLAP_WORDS:
        return [" ".join(words)]
    step = CHUNK_WORDS - OVERLAP_WORDS
    return [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, len(words) - OVERLAP_WORDS, step)]


def chunk_case(c: dict) -> list[dict]:
    """Passages d'environ 170 mots (chevauchement 35), sans franchir les limites de zones.

    Le sommaire officiel (résumé de la règle de droit) et les thèmes forment un passage
    à part : c'est souvent lui qui répond le mieux à une recherche par notion juridique.
    """
    chunks = []
    if c.get("summary") or c.get("themes"):
        txt = (c.get("summary") or "").strip()
        if c.get("themes"):
            txt += ("\n" if txt else "") + "Thèmes : " + " ; ".join(c["themes"])
        chunks.append({"section": "sommaire", "text": txt})
    for sec in case_sections(c):
        if sec["section"] not in INDEXED_SECTIONS:
            continue
        for w in _windows(sec["text"].split()):
            chunks.append({"section": sec["section"], "text": w})
    for i, ch in enumerate(chunks):
        ch["chunk_id"] = f"{c['id']}#{i}"
        ch["case_id"] = c["id"]
    return chunks
