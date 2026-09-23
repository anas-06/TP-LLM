"""Validation côté code de la sortie du modèle.

Trois niveaux de contrôle, mesurés séparément :
1. JSON valide ?        (json.loads sur la sortie brute, puis récupération tolérante)
2. Schéma respecté ?    ({"relations": [{"subject", "relation", "object"}]})
3. Contenu plausible ?  - relation appartenant au schéma autorisé
                        - sujet et objet présents dans le texte (ancrage) ;
                          sinon l'information est considérée comme inventée.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from .relations import canonical_label

KEY_ALIASES = {
    "subject": ("subject", "head", "source", "entity1", "subj", "from"),
    "relation": ("relation", "predicate", "relation_type", "type", "rel", "property"),
    "object": ("object", "tail", "target", "entity2", "obj", "to"),
}


class Relation(BaseModel):
    subject: str
    relation: str
    object: str


class ExtractionOutput(BaseModel):
    relations: list[Relation]


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_article(norm: str) -> str:
    return re.sub(r"^(the|a|an) ", "", norm)


def is_grounded(entity: str, text_norm: str) -> bool:
    n = normalize(entity)
    if not n:
        return False
    padded = f" {text_norm} "
    return f" {n} " in padded or f" {strip_article(n)} " in padded


def _recover_json(raw: str) -> Any:
    """Tente de récupérer un objet JSON dans une sortie « bruitée »."""
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch in "{[":
            try:
                obj, _ = decoder.raw_decode(cleaned[i:])
            except json.JSONDecodeError:
                continue
            # Un sous-objet isolé (une seule relation) n'est pas la réponse :
            # on laisse alors _salvage_objects récupérer toutes les relations.
            if isinstance(obj, list) or (isinstance(obj, dict) and {"relations", "triples"} & obj.keys()):
                return obj
    raise ValueError("aucun JSON récupérable")


def _salvage_objects(raw: str) -> list[dict]:
    """Récupère les objets {...} complets d'une sortie tronquée.

    Cas fréquent avec un petit modèle : il boucle jusqu'à la limite de tokens
    et le JSON final n'est jamais fermé ; les relations déjà écrites restent
    exploitables.
    """
    objs = []
    for m in re.finditer(r"\{[^{}]*\}", raw):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def _pick(item: dict, field: str) -> str | None:
    for key in KEY_ALIASES[field]:
        if key in item and isinstance(item[key], (str, int, float)):
            return str(item[key]).strip()
    return None


def parse_output(raw: str) -> dict:
    """Analyse la sortie brute : validité JSON / schéma et liste de relations."""
    report: dict[str, Any] = {"json_valid": False, "json_recovered": False,
                              "schema_valid": False, "errors": [], "reasoning": None,
                              "items": []}
    try:
        data = json.loads(raw)
        report["json_valid"] = True
        report["json_recovered"] = True
    except json.JSONDecodeError as exc:
        report["errors"].append(f"JSON invalide : {exc.msg}")
        try:
            data = _recover_json(raw)
            report["json_recovered"] = True
            report["errors"].append("JSON récupéré après nettoyage")
        except ValueError:
            salvaged = _salvage_objects(raw)
            if not salvaged:
                return report
            data = {"relations": salvaged}
            report["json_recovered"] = True
            report["errors"].append(f"JSON tronqué : {len(salvaged)} objet(s) complet(s) récupéré(s)")

    try:
        ExtractionOutput.model_validate(data)
        report["schema_valid"] = True
    except ValidationError as exc:
        report["errors"].append(f"Schéma non respecté : {exc.error_count()} erreur(s)")

    if isinstance(data, dict):
        if isinstance(data.get("reasoning"), (str, list)):
            report["reasoning"] = data["reasoning"]
        items = data.get("relations", data.get("triples", []))
    elif isinstance(data, list):
        items = data
    else:
        items = []
    if not isinstance(items, list):
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue
        s, r, o = _pick(item, "subject"), _pick(item, "relation"), _pick(item, "object")
        if s and r and o:
            report["items"].append({"subject": s, "relation": r, "object": o})
    return report


def validate_relations(items: list[dict], text: str, allowed_labels: list[str],
                       drop_invalid: bool = True) -> tuple[list[dict], list[dict]]:
    """Normalise et contrôle chaque relation.

    Renvoie (relations retenues, relations rejetées). Chaque relation porte
    ses indicateurs de contrôle (grounded, known_relation, ...).
    """
    text_norm = normalize(text)
    allowed = set(allowed_labels)
    kept, rejected, seen = [], [], set()
    for it in items:
        rel = {
            "subject": it["subject"],
            "relation": canonical_label(it["relation"]),
            "raw_relation": it["relation"],
            "object": it["object"],
        }
        rel["subject_grounded"] = is_grounded(rel["subject"], text_norm)
        rel["object_grounded"] = is_grounded(rel["object"], text_norm)
        rel["grounded"] = rel["subject_grounded"] and rel["object_grounded"]
        rel["known_relation"] = rel["relation"] in allowed
        key = (normalize(rel["subject"]), rel["relation"], normalize(rel["object"]))
        problems = []
        if not rel["grounded"]:
            problems.append("entité absente du texte")
        if not rel["known_relation"]:
            problems.append("relation hors schéma")
        if key[0] == key[2]:
            problems.append("sujet = objet")
        if key in seen:
            problems.append("doublon")
        rel["problems"] = problems
        seen.add(key)
        if problems and drop_invalid:
            rejected.append(rel)
        else:
            kept.append(rel)
    return kept, rejected
