"""Pipeline d'extraction : prompt -> appel LLM -> analyse -> validation."""

from __future__ import annotations

import json

from .llm_client import GenerationParams, LLMClient
from .prompts import (STRATEGIES, build_entity_messages, build_messages,
                      build_relation_messages)
from .relations import labels_for
from .validation import is_grounded, normalize, parse_output, validate_relations


def _parse_entities(raw: str, text: str) -> tuple[list[dict], bool]:
    try:
        data = json.loads(raw)
        ok = True
    except json.JSONDecodeError:
        return [], False
    items = data.get("entities", []) if isinstance(data, dict) else data
    tn = normalize(text)
    entities, seen = [], set()
    for it in items if isinstance(items, list) else []:
        name = it.get("name") if isinstance(it, dict) else it if isinstance(it, str) else None
        if not name or not is_grounded(str(name), tn) or normalize(str(name)) in seen:
            continue
        seen.add(normalize(str(name)))
        entities.append({"name": str(name), "type": it.get("type", "?") if isinstance(it, dict) else "?"})
    return entities, ok


def run_extraction(client: LLMClient, text: str, strategy: str, params: GenerationParams,
                   codes: list[str], n_shots: int = 3, drop_invalid: bool = True) -> dict:
    if strategy not in STRATEGIES:
        raise ValueError(f"stratégie inconnue : {strategy}")
    calls = []
    entities = None
    if strategy == "decompose":
        ent_msgs = build_entity_messages(text, codes, n_shots)
        ent_resp = client.chat(ent_msgs, params)
        entities, ent_ok = _parse_entities(ent_resp.content, text)
        calls.append({"step": "entities", "messages": ent_msgs, "output": ent_resp.content,
                      "json_valid": ent_ok, **_stats(ent_resp)})
        messages = build_relation_messages(text, entities, codes, n_shots)
    else:
        messages = build_messages(strategy, text, codes, n_shots)

    resp = client.chat(messages, params)
    calls.append({"step": "relations", "messages": messages, "output": resp.content, **_stats(resp)})

    report = parse_output(resp.content)
    kept, rejected = validate_relations(report["items"], text, labels_for(codes), drop_invalid)
    return {
        "strategy": strategy,
        "params": params.model_dump(),
        "model": client.model,
        "relations": kept,
        "rejected": rejected,
        "entities": entities,
        "reasoning": report["reasoning"],
        "json_valid": report["json_valid"],
        "json_recovered": report["json_recovered"],
        "schema_valid": report["schema_valid"],
        "errors": report["errors"],
        "n_raw_relations": len(report["items"]),
        "latency_s": round(sum(c["latency_s"] for c in calls), 3),
        "calls": calls,
    }


def _stats(resp) -> dict:
    return {"latency_s": resp.latency_s, "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens}
