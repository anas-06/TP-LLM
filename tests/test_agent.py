"""Tests du TP2 (corpus, index, recherche) et du TP3 (boucle agentique) avec un faux LLM.

Ils utilisent le corpus et l'index locaux (data/juris) : ils sont ignorés s'ils sont absents.
"""

import pytest

from backend.juris.corpus import CASES_PATH, chunk_case, find_case
from backend.juris.index import INDEX_DIR, tokenize

pytestmark = pytest.mark.skipif(not (CASES_PATH.exists() and (INDEX_DIR / "bm25.npz").exists()),
                                reason="corpus/index absents : python -m scripts.prepare_juris && python -m scripts.build_index")

CASE_NUM = "23-20.161"


# ------------------------------------------------------------------ TP2
def test_tokenize_french():
    toks = tokenize("Les salariés protégés et l'Inspecteur du travail")
    assert "les" not in toks and "et" not in toks
    assert all(t.isascii() for t in toks)
    assert tokenize("salariés")[0] == tokenize("salarié")[0]  # racinisation


def test_chunks_follow_sections():
    c = find_case(CASE_NUM)
    chunks = chunk_case(c)
    assert chunks[0]["chunk_id"] == f"{c['id']}#0"
    assert {ch["section"] for ch in chunks} <= {"sommaire", "introduction", "expose", "moyens", "motivations", "dispositif"}
    assert any(ch["section"] == "dispositif" and "REJETTE" in ch["text"] for ch in chunks)


def test_search_by_number_and_keywords():
    from backend.juris.rag import search_cases
    r = search_cases(CASE_NUM, k=3, mode="bm25")
    assert r["results"][0]["number"] == CASE_NUM and r["results"][0]["exact_match"]
    r = search_cases("bail rural congé reprise pour exploiter", k=5, mode="bm25")
    assert any(x["number"] == CASE_NUM for x in r["results"])


# ------------------------------------------------------------------ TP3
class FakeLLM:
    """Remplace llm_json : une file de réponses par usage (décision, réponse, vérification)."""

    def __init__(self, **queues):
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls = []

    def __call__(self, system, user, *, purpose, usage=None, **kw):
        key = {"décision": "plan", "réponse": "answer", "vérification": "judge"}[purpose]
        self.calls.append((key, user))
        q = self.queues.get(key) or [{}]
        out = q.pop(0) if len(q) > 1 else q[0]
        return out, str(out)


def run_agent(monkeypatch, fake, question, **opts):
    from backend.agent import graph
    monkeypatch.setattr(graph, "llm_json", fake)
    events = []
    ctx = graph.RunContext(events.append, llm_check=opts.pop("llm_check", True))
    state = {"question": question, "memory": {}, "evidence": [], "candidates": [], "searches": [],
             "read_cases": [], "external_used": False, "external_allowed": opts.pop("external", False),
             "iteration": 0, "max_iterations": opts.pop("max_iterations", 5), "feedback": None, "draft": None}
    path, final = [], dict(state)
    for upd in graph.GRAPH.stream(state, {"configurable": {"ctx": ctx}}, stream_mode="updates"):
        for node, values in upd.items():
            path.append(node)
            final.update(values or {})
    return final, events, path


QUESTION = f"Dans l'affaire n° {CASE_NUM}, qu'a décidé la Cour sur le congé pour reprise ?"


def test_agent_search_read_answer(monkeypatch):
    fake = FakeLLM(
        plan=[{"action": "search_cases", "args": {"query": CASE_NUM}, "reason": "chercher"},
              {"action": "read_case", "args": {"case_id": "D1"}, "reason": "lire"},
              {"action": "answer", "args": {}, "reason": "assez de preuves"}],
        answer=[{"answer": "La Cour rejette le pourvoi : le preneur n'est tenu de mettre en cause que le bailleur [C5].",  # C1-C4 : recherche, C5+ : lecture
                 "citations": ["C5"], "found": True}],
        judge=[{"supported": True, "unsupported_claims": [], "missing": ""}],
    )
    final, events, path = run_agent(monkeypatch, fake, QUESTION)
    assert path == ["agent", "tools", "agent", "tools", "agent", "answer", "verify", "final"]
    types = [e["type"] for e in events if e["type"] != "NODE"]
    for t in ("DECISION", "TOOL", "OBSERVATION", "GENERATION", "CHECK", "FINAL"):
        assert t in types
    assert final["final"]["status"] == "vérifiée"
    assert final["final"]["sources"]["corpus"][0]["title"].endswith(CASE_NUM)
    assert final["final"]["metrics"]["tool_calls"] == 2


def test_guardrails_invalid_action_and_answer_without_evidence(monkeypatch):
    fake = FakeLLM(plan=[{"action": "ordonnance"}, {"action": "answer"}, {"action": "answer"}],
                   answer=[{"answer": "Rejet du pourvoi [C1].", "citations": ["C1"], "found": True}],
                   judge=[{"supported": True}])
    final, events, path = run_agent(monkeypatch, fake, "bail rural congé reprise pour exploiter", llm_check=False)
    decisions = [e for e in events if e["type"] == "DECISION"]
    assert decisions[0]["data"]["action"] == "search_cases" and "invalide" in decisions[0]["data"]["guardrail"]
    assert path[:2] == ["agent", "tools"]


def test_empty_action_reads_case_cited_by_number(monkeypatch):
    """Cas observé pendant la démo : action vide après la recherche -> on lit l'affaire citée."""
    fake = FakeLLM(plan=[{"action": "search_cases", "args": {"query": CASE_NUM}}, {}, {"action": "answer"}],
                   answer=[{"answer": "Rejet du pourvoi [C5].", "citations": ["C5"], "found": True}],
                   judge=[{"supported": True}])
    final, events, path = run_agent(monkeypatch, fake, QUESTION, llm_check=False)
    second = [e for e in events if e["type"] == "DECISION"][1]
    assert second["data"]["action"] == "read_case"
    assert path[:4] == ["agent", "tools", "agent", "tools"]


def test_verifier_failure_loops_then_stops_at_limit(monkeypatch):
    fake = FakeLLM(
        plan=[{"action": "search_cases", "args": {"query": "bail rural congé"}},
              {"action": "answer"}],
        answer=[{"answer": "La Lune est faite de fromage selon la Cour [C9].", "citations": ["C9"], "found": True}],
        judge=[{"supported": False, "unsupported_claims": ["La Lune est faite de fromage"]}],
    )
    final, events, path = run_agent(monkeypatch, fake, "bail rural congé", max_iterations=3)
    checks = [e for e in events if e["type"] == "CHECK"]
    assert checks and not checks[0]["data"]["ok"]
    assert path.count("agent") >= 2  # retour à l'agent après l'échec de la vérification
    assert final["final"]["status"] in {"non vérifiée", "arrêt"}
    assert final["final"]["warning"]


def test_tool_error_is_visible_and_handled(monkeypatch):
    from backend.agent import graph
    monkeypatch.setattr(graph, "tool_search_cases",
                        lambda q, k=5: (_ for _ in ()).throw(graph.ToolError("index indisponible")))
    fake = FakeLLM(plan=[{"action": "search_cases", "args": {"query": "bail"}}])
    final, events, path = run_agent(monkeypatch, fake, "bail rural", max_iterations=2)
    errors = [e for e in events if e["type"] == "ERROR"]
    assert errors and "index indisponible" in errors[0]["data"]["error"]
    assert final["final"]["status"] == "arrêt"  # arrêt contrôlé, sans exception
