import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.dataset import doc_text, filter_relations, load_split
from backend.extraction import run_extraction
from backend.llm_client import GenerationParams, LLMClient, LLMResponse
from backend.metrics import aggregate, score_document
from backend.prompts import STRATEGIES, build_messages, select_demos
from backend.relations import CORE_RELATIONS, canonical_label, labels_for, resolve_relation_set
from backend.validation import is_grounded, normalize, parse_output, validate_relations

TEXT = "Alice Martin studied at Stanford University. After graduating, she joined Google in 2018."


class FakeClient:
    """Remplace le LLM : renvoie des réponses scriptées et garde les messages reçus."""
    model = "fake"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.received = []

    def chat(self, messages, params):
        self.received.append((messages, params))
        return LLMResponse(content=self.outputs.pop(0), latency_s=0.01)


# ------------------------------------------------------------------ validation

def test_canonical_label():
    assert canonical_label("Employed by") == "employer"
    assert canonical_label("educated_at") == "educated_at"
    assert canonical_label("P69") == "educated_at"
    assert canonical_label("studied-at") == "educated_at"


def test_grounding():
    tn = normalize(TEXT)
    assert is_grounded("Stanford University", tn)
    assert is_grounded("the Stanford University", tn)
    assert not is_grounded("Harvard", tn)
    assert not is_grounded("Goo", tn)  # pas de correspondance partielle de mot


def test_parse_valid_and_recovered():
    ok = parse_output('{"relations": [{"subject": "A", "relation": "r", "object": "B"}]}')
    assert ok["json_valid"] and ok["schema_valid"] and len(ok["items"]) == 1

    noisy = parse_output('Here you go:\n```json\n{"relations": [{"head": "A", "relation": "r", "tail": "B"}]}\n```')
    assert not noisy["json_valid"] and noisy["json_recovered"]
    assert not noisy["schema_valid"]  # clés head/tail hors schéma...
    assert noisy["items"] == [{"subject": "A", "relation": "r", "object": "B"}]  # ...mais récupérées

    bad = parse_output('{"relations": [')
    assert not bad["json_recovered"] and bad["items"] == []


def test_validate_relations_rejects_hallucinations():
    items = [
        {"subject": "Alice Martin", "relation": "employed_by", "object": "Google"},
        {"subject": "Alice Martin", "relation": "educated_at", "object": "Harvard"},
        {"subject": "Alice Martin", "relation": "likes", "object": "Google"},
        {"subject": "Alice Martin", "relation": "employer", "object": "Google"},
    ]
    kept, rejected = validate_relations(items, TEXT, labels_for(CORE_RELATIONS))
    assert [(r["subject"], r["relation"], r["object"]) for r in kept] == [("Alice Martin", "employer", "Google")]
    problems = [r["problems"] for r in rejected]
    assert ["entité absente du texte"] in problems
    assert ["relation hors schéma"] in problems
    assert ["doublon"] in problems


# ------------------------------------------------------------------ données / prompts

def test_dataset_and_demos_are_disjoint():
    eval_ids = {d["id"] for d in load_split("eval")}
    demos = select_demos(CORE_RELATIONS, 5)
    assert len(demos) == 5
    assert not eval_ids & {d["id"] for d in demos}
    for demo in demos:  # les réponses de démonstration sont ancrées dans leur texte
        for t in demo["triples"]:
            assert is_grounded(t["subject"], normalize(demo["text"]))
            assert is_grounded(t["object"], normalize(demo["text"]))


@pytest.mark.parametrize("strategy", ["zero_shot", "context", "few_shot", "cot"])
def test_build_messages(strategy):
    msgs = build_messages(strategy, TEXT, CORE_RELATIONS, n_shots=2)
    assert msgs[-1]["role"] == "user" and "Alice Martin" in msgs[-1]["content"]
    n_demo = sum(m["role"] == "assistant" for m in msgs)
    assert n_demo == (2 if strategy in ("few_shot", "cot") else 0)
    if strategy == "zero_shot":
        assert "educated_at" not in msgs[0]["content"]
    else:
        assert "educated_at" in msgs[0]["content"]
    for m in msgs:
        if m["role"] == "assistant":
            json.loads(m["content"])


def test_relation_set_resolution():
    assert resolve_relation_set("core") == CORE_RELATIONS
    assert len(resolve_relation_set("all")) == 96
    assert resolve_relation_set("educated_at,P108") == ["P69", "P108"]
    with pytest.raises(ValueError):
        resolve_relation_set("foo")


# ------------------------------------------------------------------ pipeline

GOOD = json.dumps({"relations": [
    {"subject": "Alice Martin", "relation": "educated_at", "object": "Stanford University"},
    {"subject": "Alice Martin", "relation": "employed_by", "object": "Google"},
    {"subject": "Alice Martin", "relation": "spouse", "object": "Bob"},
]})


@pytest.mark.parametrize("strategy", [s for s in STRATEGIES if s != "decompose"])
def test_run_extraction_single_call(strategy):
    fake = FakeClient([GOOD])
    params = GenerationParams(temperature=0.3, seed=7)
    res = run_extraction(fake, TEXT, strategy, params, CORE_RELATIONS)
    assert res["json_valid"] and res["schema_valid"]
    assert {(r["relation"], r["object"]) for r in res["relations"]} == {
        ("educated_at", "Stanford University"), ("employer", "Google")}
    assert res["rejected"][0]["object"] == "Bob"
    assert fake.received[0][1].temperature == 0.3


def test_run_extraction_decompose():
    ents = json.dumps({"entities": [{"name": "Alice Martin", "type": "PER"},
                                    {"name": "Google", "type": "ORG"},
                                    {"name": "Mars", "type": "LOC"}]})
    fake = FakeClient([ents, GOOD])
    res = run_extraction(fake, TEXT, "decompose", GenerationParams(), CORE_RELATIONS)
    assert [e["name"] for e in res["entities"]] == ["Alice Martin", "Google"]  # Mars non ancré
    assert "- Google (ORG)" in fake.received[1][0][-1]["content"]
    assert len(res["calls"]) == 2 and len(res["relations"]) == 2


def test_scoring_against_gold():
    doc = filter_relations(load_split("eval")[0], CORE_RELATIONS)
    gold = doc["triples"][0]
    ent_s = doc["entities"][gold["subject_id"]]["names"][-1]  # une autre mention compte aussi
    pred = [{"subject": ent_s, "relation": gold["relation"], "object": gold["object"],
             "grounded": True, "known_relation": True},
            {"subject": "Nobody", "relation": "spouse", "object": "Rihanna",
             "grounded": False, "known_relation": True}]
    row = score_document(pred, pred, doc)
    assert row["tp_pred"] == 1 and row["n_pred"] == 2 and row["n_ungrounded"] == 1
    agg = aggregate([{**row, "json_valid": True, "schema_valid": True, "latency_s": 1.0}])
    assert agg["precision"] == 0.5 and 0 < agg["recall"] <= 1


# ------------------------------------------------------------------ client HTTP / API

def test_ollama_payload_contains_generation_params():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "{}"}, "eval_count": 3})

    client = LLMClient("ollama", "http://x", "llama3.2:1b")
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client.chat([{"role": "user", "content": "hi"}], GenerationParams(temperature=0.7, seed=3))
    assert seen["format"] == "json" and seen["model"] == "llama3.2:1b"
    assert seen["options"]["temperature"] == 0.7 and seen["options"]["seed"] == 3


def test_api_extract_with_example(monkeypatch):
    doc = filter_relations(load_split("eval")[0], CORE_RELATIONS)
    t = doc["triples"][0]
    out = json.dumps({"relations": [{"subject": t["subject"], "relation": t["relation"], "object": t["object"]}]})
    monkeypatch.setattr(main, "client", FakeClient([out]))
    api = TestClient(main.app)
    r = api.post("/api/extract", json={"example_id": doc["id"], "strategy": "few_shot"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["score"]["precision"] == 1.0 and body["relations"][0]["correct"]
    assert body["text"] == doc_text(doc)


def test_api_misc(monkeypatch):
    api = TestClient(main.app)
    assert api.get("/").status_code == 200
    cfg = api.get("/api/config").json()
    assert set(cfg["strategies"]) == set(STRATEGIES)
    assert api.post("/api/extract", json={"text": ""}).status_code == 422
    monkeypatch.setattr(main, "client", FakeClient([GOOD] * 4))
    rows = api.post("/api/compare", json={"text": TEXT, "strategies": ["context", "few_shot"],
                                          "temperatures": [0, 0.7]}).json()
    assert [(r["strategy"], r["params"]["temperature"]) for r in rows] == [
        ("context", 0), ("context", 0.7), ("few_shot", 0), ("few_shot", 0.7)]
