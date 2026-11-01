"""Boucle agentique (LangGraph).

          ┌──────────────── retour si preuves insuffisantes ───────────────┐
          ▼                                                                │
  START → agent ──(search_cases | read_case | external_search)──► tools ──┘ (retour à agent)
            │
            └──(answer)──► answer ──► verify ──(ok ou limite)──► final → END
                                         └──(échec)──► agent

- agent  : le LLM choisit la prochaine action en JSON ; des garde-fous en code corrigent
           les choix invalides (action inconnue, recherche répétée, réponse sans preuve…).
- tools  : exécute l'outil, ajoute l'observation et les preuves à l'état.
- answer : rédige la réponse à partir des preuves numérotées [C1] (corpus) / [E1] (externe).
- verify : présence de preuves, citations valides, cohérence réponse/passages (contrôle
           lexical + juge LLM), absence d'information non sourcée.
- final  : réponse + sources, ou arrêt contrôlé (limite d'étapes, erreur).

Chaque nœud publie ses événements de trace en direct via `RunContext.emit`.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from ..juris.corpus import find_case
from ..juris.index import get_index, tokenize
from ..juris.llm import LLMError, Usage, llm_json
from .tools import ToolError, tool_external_search, tool_read_case, tool_search_cases

MAX_ITERATIONS_DEFAULT = 5


class AgentState(TypedDict, total=False):
    question: str
    memory: dict             # affaire et échange précédents (bonus mémoire)
    evidence: list[dict]     # preuves citables, pid C1.. (corpus) / E1.. (externe)
    candidates: list[dict]   # décisions trouvées, étiquetées D1, D2…
    searches: list[str]
    read_cases: list[str]
    external_used: bool
    external_allowed: bool
    iteration: int
    max_iterations: int
    action: dict | None
    feedback: str | None     # message du vérificateur
    draft: dict | None
    verification: dict | None
    status: str
    final: dict | None


class RunContext:
    """Ce qui ne fait pas partie de l'état métier : trace, compteurs, validation humaine."""

    def __init__(self, emit: Callable[[dict], None], *, approval: Callable[[dict], bool] | None = None,
                 llm_check: bool = True, model: str | None = None) -> None:
        self._emit = emit
        self.approval = approval
        self.llm_check = llm_check
        self.model = model
        self.usage = Usage()
        self.tool_calls = 0
        self.tool_errors = 0
        self.t0 = time.perf_counter()
        self.step = 0

    def emit(self, type_: str, title: str, node: str, **data: Any) -> None:
        self.step += 1
        self._emit({"step": self.step, "type": type_, "title": title, "node": node,
                    "t": round(time.perf_counter() - self.t0, 2), "data": data})

    def metrics(self) -> dict:
        return {**self.usage.to_dict(), "tool_calls": self.tool_calls, "tool_errors": self.tool_errors,
                "total_seconds": round(time.perf_counter() - self.t0, 1)}


def _ctx(config) -> RunContext:
    return config["configurable"]["ctx"]


# ------------------------------------------------------------------ nœud agent
PLANNER_SYSTEM = """Tu es l'agent de recherche d'un assistant juridique (jurisprudence de la Cour de cassation).
Tu ne réponds pas toi-même : tu choisis la PROCHAINE action parmi :
- search_cases {"query": "mots-clés juridiques", "k": 5} : chercher des décisions dans le corpus.
- read_case {"case_id": "D1"} : lire une décision déjà trouvée (motivations, dispositif). À utiliser quand
  la question vise une affaire précise ou quand l'extrait trouvé ne suffit pas pour répondre.
{external}- answer {} : rédiger la réponse, quand les preuves collectées suffisent.
Règles :
- Sans aucune preuve, commence par search_cases.
- Si la question cite un numéro de pourvoi et que la décision a été trouvée, lis-la (read_case) puis réponds.
- Ne refais pas une recherche déjà faite ; si une recherche a été insuffisante, change les mots-clés.
- Ne relis pas une décision déjà lue.
- Choisis answer dès que les preuves suffisent : n'ajoute pas d'étapes inutiles.
Réponds uniquement en JSON : {"reason": "justification en une phrase", "action": "...", "args": {...}}"""

EXTERNAL_LINE = ("- external_search {\"query\": \"notion\"} : source externe (Wikipédia), seulement pour une "
                 "notion générale absente du corpus ou si le corpus ne donne rien.\n")


def _planner_prompt(s: AgentState) -> str:
    lines = [f"Question : {s['question']}"]
    mem = s.get("memory") or {}
    if mem.get("case_id"):
        lines.append(f"Affaire de l'échange précédent : D0 = {mem.get('title')} "
                     f"(question précédente : {mem.get('last_question')})")
    lines.append(f"Étape {s.get('iteration', 0) + 1} sur {s.get('max_iterations', MAX_ITERATIONS_DEFAULT)}.")
    lines.append("Recherches déjà faites : " + ("; ".join(f'"{q}"' for q in s.get("searches", [])) or "aucune"))
    cands = s.get("candidates", [])[-8:]
    if cands:
        lines.append("Décisions trouvées :")
        for c in cands:
            lu = " [DÉJÀ LUE, ses passages sont dans les preuves]" if c["case_id"] in s.get("read_cases", []) else ""
            ex = " [numéro de pourvoi cité dans la question]" if c.get("exact") else ""
            lines.append(f"  {c['label']} = {c['title']} — {c['solution']}{ex}{lu} — {c['snippet'][:160]}")
    ev = s.get("evidence", [])
    n_c = sum(e["source"] == "corpus" for e in ev)
    n_e = len(ev) - n_c
    lines.append(f"Preuves collectées : {n_c} passage(s) du corpus, {n_e} source(s) externe(s).")
    if s.get("external_used"):
        lines.append("La source externe a déjà été consultée.")
    if s.get("feedback"):
        lines.append(f"Retour du vérificateur sur la réponse précédente : {s['feedback']}")
    return "\n".join(lines)


def _resolve_case(ref: str, s: AgentState) -> str | None:
    ref = str(ref or "").strip()
    mem = s.get("memory") or {}
    if ref.upper() == "D0" and mem.get("case_id"):
        return mem["case_id"]
    for c in s.get("candidates", []):
        if ref.upper() == c["label"] or ref == c["case_id"]:
            return c["case_id"]
    c = find_case(ref)
    return c["id"] if c else None


def _similar(a: str, b: str) -> bool:
    """Deux requêtes sont jugées identiques si leurs mots (racinisés) se recouvrent à 70 % (Jaccard)."""
    ta, tb = set(tokenize(a)), set(tokenize(b))
    return bool(ta | tb) and len(ta & tb) / len(ta | tb) >= 0.7


def agent_node(state: AgentState, config) -> dict:
    ctx = _ctx(config)
    ctx.emit("NODE", "Agent : choix de la prochaine action", "agent")
    it = state.get("iteration", 0)
    max_it = state.get("max_iterations", MAX_ITERATIONS_DEFAULT)
    ev = state.get("evidence", [])

    if it >= max_it:
        reason = f"Limite de {max_it} étapes atteinte"
        if ev:
            action = {"action": "answer", "args": {}, "reason": reason + " : réponse avec les preuves disponibles."}
        else:
            action = {"action": "stop", "args": {}, "reason": reason + " sans preuve : arrêt contrôlé."}
        ctx.emit("DECISION", action["reason"], "agent", action=action["action"], args={}, guardrail="limite d'étapes")
        return {"action": action, "iteration": it}

    system = PLANNER_SYSTEM.replace("{external}", EXTERNAL_LINE if state.get("external_allowed") else "")
    guard = None
    try:
        obj, raw = llm_json(system, _planner_prompt(state), purpose="décision", usage=ctx.usage,
                            max_tokens=160, model=ctx.model)
    except LLMError as exc:
        ctx.emit("ERROR", "LLM injoignable", "agent", error=str(exc))
        return {"action": {"action": "stop", "args": {}, "reason": "LLM injoignable"}, "status": "erreur LLM"}

    obj = obj or {}
    action = str(obj.get("action") or "").strip()
    args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
    reason = str(obj.get("reason") or "").strip()[:240]
    allowed = {"search_cases", "read_case", "answer"} | ({"external_search"} if state.get("external_allowed") else set())

    # ---- garde-fous : le code corrige les décisions invalides du petit modèle
    read = state.get("read_cases", [])
    unread = [c for c in state.get("candidates", []) if c["case_id"] not in read]
    unread.sort(key=lambda c: not c.get("exact"))  # la décision citée par son numéro d'abord
    exact_unread = [c for c in unread if c.get("exact")]
    if exact_unread and action in {"search_cases", "answer", "external_search"}:
        guard = "la décision citée par son numéro a été trouvée mais pas encore lue"
        action, args = "read_case", {"case_id": exact_unread[0]["label"]}
    if action not in allowed:
        guard = f"action invalide « {action or '∅'} »"
        action, args = ("search_cases", {"query": state["question"]}) if not ev else ("answer", {})
    if action == "answer" and not ev:
        guard, action, args = "réponse demandée sans aucune preuve", "search_cases", {"query": state["question"]}
    if action == "answer" and state.get("feedback") and state.get("draft") and \
            len(ev) == state["draft"].get("n_evidence", -1):
        guard = "le vérificateur a refusé la réponse et aucune nouvelle preuve n'a été ajoutée"
        if unread:
            action, args = "read_case", {"case_id": unread[0]["label"]}
        elif state.get("external_allowed") and not state.get("external_used"):
            action, args = "external_search", {"query": state["question"]}
        else:
            action, args = "search_cases", {"query": f"{state['question']} {state.get('feedback', '')}"[:200]}
    if action == "search_cases" and state.get("searches") and ev and not state.get("feedback"):
        # Pas de nouvelle recherche « par précaution » : des preuves existent, on rédige et
        # c'est le vérificateur qui dira si elles suffisent (évite les étapes inutiles).
        guard = "des preuves existent déjà : le vérificateur jugera si elles suffisent"
        action, args = "answer", {}
    if action == "search_cases":
        q = str(args.get("query") or state["question"]).strip()
        if any(_similar(q, x) for x in state.get("searches", [])):
            guard = "recherche identique ou quasi identique à une recherche déjà faite"
            if read and not state.get("feedback"):
                action, args = "answer", {}  # une décision a déjà été lue : on rédige
            elif state.get("feedback") and state.get("external_allowed") and not state.get("external_used"):
                # le corpus n'a pas suffi et la recherche tourne en rond : source externe
                action, args = "external_search", {"query": state["question"]}
            elif unread:
                action, args = "read_case", {"case_id": unread[0]["label"]}
            elif state.get("external_allowed") and not state.get("external_used"):
                action, args = "external_search", {"query": state["question"]}
            else:  # plus rien à essayer : on rédige avec les preuves, ou arrêt contrôlé sans preuve
                action, args = ("answer", {}) if ev else ("stop", {})
        else:
            args = {"query": q, "k": int(args.get("k") or 5) if str(args.get("k", "5")).isdigit() else 5}
    if action == "read_case":
        cid = _resolve_case(args.get("case_id"), state)
        if cid in read:
            guard, cid = "décision déjà lue", None
            if state.get("feedback") and unread:  # le vérificateur a demandé plus de preuves
                cid = unread[0]["case_id"]
        elif not cid:
            guard = f"identifiant de décision inconnu « {args.get('case_id')} »"
            cid = unread[0]["case_id"] if unread else None
        if cid:
            label = next((c["label"] for c in state.get("candidates", []) if c["case_id"] == cid), "D0")
            args = {"case_id": cid, "label": label}
        else:
            action, args = ("answer", {}) if ev else ("search_cases", {"query": state["question"]})
    if action == "external_search" and state.get("external_used"):
        guard, action, args = "source externe déjà consultée", ("answer" if ev else "search_cases"), \
            ({} if ev else {"query": state["question"]})
    if action == "external_search":
        args = {"query": str(args.get("query") or state["question"])[:200]}

    action_obj = {"action": action, "args": args, "reason": reason or "(pas de justification fournie)"}
    title = {"search_cases": "Rechercher dans le corpus", "read_case": "Lire une décision",
             "external_search": "Consulter la source externe", "answer": "Rédiger la réponse", "stop": "Arrêter (aucune preuve exploitable)"}.get(action, action)
    ctx.emit("DECISION", title, "agent", action=action, args=args, reason=action_obj["reason"],
             guardrail=guard, raw=None if obj else raw)
    return {"action": action_obj, "iteration": it + 1}


def route_agent(state: AgentState) -> str:
    a = (state.get("action") or {}).get("action")
    if a == "answer":
        return "answer"
    if a == "stop":
        return "final"
    return "tools"


# ------------------------------------------------------------------ nœud outils
def _add_evidence(ev: list[dict], new: list[dict]) -> list[dict]:
    ev = list(ev)
    seen = {e.get("chunk_id") or e.get("url") for e in ev}
    n_c = sum(e["source"] == "corpus" for e in ev)
    n_e = len(ev) - n_c
    for e in new:
        key = e.get("chunk_id") or e.get("url")
        if key in seen:
            continue
        seen.add(key)
        if e["source"] == "corpus":
            n_c += 1
            e = {**e, "pid": f"C{n_c}"}
        else:
            n_e += 1
            e = {**e, "pid": f"E{n_e}"}
        ev.append(e)
    return ev


def tools_node(state: AgentState, config) -> dict:
    ctx = _ctx(config)
    a = state["action"]
    name, args = a["action"], a["args"]
    ctx.emit("NODE", f"Outil : {name}", "tools")
    shown = {k: v for k, v in args.items() if k != "label"}
    if name == "read_case":
        c = find_case(args["case_id"])
        shown = {"case_id": args.get("label", "D0"), "pourvoi": c.get("number") if c else None}
    ctx.emit("TOOL", f"{name}({', '.join(f'{k}={v!r}' for k, v in shown.items())})", "tools", tool=name, args=shown)
    update: dict = {}

    if name == "external_search" and ctx.approval is not None:
        ctx.emit("APPROVAL", "Validation demandée avant l'appel à l'API externe", "tools", tool=name, args=shown)
        if not ctx.approval({"tool": name, "args": shown}):
            ctx.emit("OBSERVATION", "Appel externe refusé par l'utilisateur", "tools", tool=name, refused=True)
            return {"external_used": True, "external_allowed": False, "feedback": None}

    ctx.tool_calls += 1
    t = time.perf_counter()
    try:
        if name == "search_cases":
            out = tool_search_cases(args["query"], args.get("k", 5))
            cands = list(state.get("candidates", []))
            known = {c["case_id"] for c in cands}
            for r in out["results"]:
                if r["case_id"] not in known:
                    cands.append({**r, "label": f"D{len(cands) + 1}"})
                    known.add(r["case_id"])
            labels = {c["case_id"]: c["label"] for c in cands}
            update.update(candidates=cands, searches=state.get("searches", []) + [args["query"]])
            observed = [{"label": labels[r["case_id"]], "title": r["title"], "solution": r["solution"],
                         "snippet": r["snippet"]} for r in out["results"]]
        elif name == "read_case":
            out = tool_read_case(args["case_id"], state["question"])
            update["read_cases"] = state.get("read_cases", []) + [args["case_id"]]
            observed = [{"section": e["section_label"], "text": e["text"][:200]} for e in out["evidence"]]
        else:
            out = tool_external_search(args["query"])
            update["external_used"] = True
            observed = out["results"]
    except ToolError as exc:
        ctx.tool_errors += 1
        ctx.emit("ERROR", f"Erreur de l'outil {name}", "tools", tool=name, error=str(exc))
        if name == "external_search":
            update["external_used"] = True
        if name == "search_cases":
            update["searches"] = state.get("searches", []) + [args.get("query", "")]
        if name == "read_case":
            update["read_cases"] = state.get("read_cases", []) + [args.get("case_id", "")]
        return update
    ev = _add_evidence(state.get("evidence", []), out["evidence"])
    new_pids = [e["pid"] for e in ev[len(state.get("evidence", [])):]]
    update["evidence"] = ev
    ctx.emit("OBSERVATION", out["summary"], "tools", tool=name, results=observed, new_evidence=new_pids,
             latency_s=round(time.perf_counter() - t, 2))
    return update


# ------------------------------------------------------------------ nœud réponse
ANSWER_SYSTEM = """Tu es un assistant juridique. Réponds à la question en français, en 3 à 6 phrases,
UNIQUEMENT à partir des preuves fournies. Traite chaque partie de la question.
- Après chaque information, cite la preuve entre crochets : [C1] pour une décision du corpus, [E1] pour la source externe.
- Mentionne la décision citée (chambre, date ou numéro de pourvoi).
- Pour une affaire précise : indique ce que le demandeur au pourvoi reprochait à l'arrêt (« fait grief »),
  la réponse de la Cour (motivations) et la solution (dispositif : rejet, cassation…).
- Une source externe [E…] sert seulement de contexte général : ne la présente jamais comme une décision de justice.
- N'invente rien. Si les preuves ne permettent pas de répondre, mets "found": false et dis ce qui manque.
Réponds en JSON : {"answer": "...", "citations": ["C1"], "found": true}"""


def _select_evidence(ev: list[dict], question: str, exact_ids: set[str], limit: int = 6) -> list[dict]:
    """Choisit les preuves données au rédacteur : les plus proches de la question,
    en privilégiant les décisions lues et celle citée par son numéro (3 passages max par décision)."""
    q = set(tokenize(question))

    def score(e: dict) -> float:
        t = set(tokenize(e["text"]))
        s = len(q & t) / (len(t) ** 0.5 + 1)
        s += 1.0 if e.get("tool") == "read_case" else 0.0
        s += 2.0 if e.get("case_id") in exact_ids else 0.0
        return s

    ext = [e for e in ev if e["source"] == "external"][:2]
    corpus = [e for e in ev if e["source"] == "corpus"]
    # Question sur une affaire précise (numéro de pourvoi) : on ne mélange pas avec d'autres affaires.
    if exact_ids and any(e["case_id"] in exact_ids and e.get("tool") == "read_case" for e in corpus):
        corpus = [e for e in corpus if e["case_id"] in exact_ids]
        cap = limit
    else:
        cap = 3
    corpus = sorted(corpus, key=score, reverse=True)
    picked, per_case = [], {}
    for e in corpus:
        if per_case.get(e["case_id"], 0) >= cap:
            continue
        per_case[e["case_id"]] = per_case.get(e["case_id"], 0) + 1
        picked.append(e)
        if len(picked) >= limit - len(ext):
            break
    order = {e["pid"]: i for i, e in enumerate(ev)}
    return sorted(picked, key=lambda e: order[e["pid"]]) + ext


def answer_node(state: AgentState, config) -> dict:
    ctx = _ctx(config)
    ctx.emit("NODE", "Rédaction de la réponse", "answer")
    exact = {c["case_id"] for c in state.get("candidates", []) if c.get("exact")}
    ev = _select_evidence(state.get("evidence", []), state["question"], exact)
    block = "\n\n".join(f"[{e['pid']}] {e['title']} ({e['section_label']}) : {e['text']}" for e in ev)
    fb = f"\n\nAttention, la réponse précédente a été refusée : {state['feedback']}" if state.get("feedback") else ""
    try:
        obj, raw = llm_json(ANSWER_SYSTEM, f"Preuves :\n{block}\n\nQuestion : {state['question']}{fb}",
                            purpose="réponse", usage=ctx.usage, max_tokens=420, model=ctx.model)
    except LLMError as exc:
        ctx.emit("ERROR", "LLM injoignable", "answer", error=str(exc))
        return {"draft": None, "status": "erreur LLM"}
    obj = obj or {}
    answer = str(obj.get("answer") or "").strip()
    cits = set(re.findall(r"\[?([CE]\d+)\]?", answer))
    cits |= {c for c in obj.get("citations", []) if isinstance(c, str)}
    draft = {"answer": answer, "citations": sorted(cits), "found": bool(obj.get("found", True)),
             "used_pids": [e["pid"] for e in ev], "n_evidence": len(state.get("evidence", [])),
             "json_valid": bool(obj)}
    ctx.emit("GENERATION", "Brouillon de réponse rédigé", "answer", answer=answer, citations=draft["citations"],
             found=draft["found"], evidence_given=draft["used_pids"])
    return {"draft": draft}


# ------------------------------------------------------------------ nœud vérification
JUDGE_SYSTEM = """Tu es un vérificateur juridique strict. On te donne des PASSAGES et une RÉPONSE.
Vérifie que chaque affirmation de la réponse est justifiée par les passages (pas par tes connaissances).
Réponds en JSON : {"supported": true, "unsupported_claims": ["..."], "missing": "ce qui manque pour bien répondre, ou vide"}"""


KEY_TERM_IDF = 6.0  # au-dessus : terme rare dans le corpus (notion juridique précise)


def _key_terms(text: str) -> set[str]:
    idx = get_index()
    top = float(idx.idf.max())
    return {t for t in tokenize(text)
            if (float(idx.idf[idx.vocab[t]]) if t in idx.vocab else top) >= KEY_TERM_IDF}


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\[[CE]\d+\]", "", text)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]


def verify_node(state: AgentState, config) -> dict:
    ctx = _ctx(config)
    ctx.emit("NODE", "Vérification des preuves", "verify")
    d = state.get("draft") or {}
    ev = {e["pid"]: e for e in state.get("evidence", [])}
    answer = d.get("answer", "")
    cited = [c for c in d.get("citations", []) if c in ev]
    invalid = [c for c in d.get("citations", []) if c not in ev]
    corpus_cited = [c for c in cited if ev[c]["source"] == "corpus"]

    checks = {
        "preuves_presentes": bool(ev) and any(e["source"] == "corpus" for e in ev.values()),
        "citations_valides": bool(cited) and not invalid,
        "source_corpus_citee": bool(corpus_cited),
        "reponse_trouvee": d.get("found", False) and bool(answer),
    }
    # La question vise une affaire précise : la réponse doit citer cette affaire-là.
    exact = {c["case_id"] for c in state.get("candidates", []) if c.get("exact")}
    if exact:
        checks["affaire_demandee_citee"] = all(ev[c]["case_id"] in exact for c in corpus_cited) and bool(corpus_cited)
    # Contrôle lexical : chaque phrase doit partager l'essentiel de ses mots avec les preuves
    # données au rédacteur (sinon elle vient des connaissances du modèle, pas des sources).
    support_vocab = set()
    for c in set(cited) | {p for p in d.get("used_pids", []) if p in ev}:
        support_vocab |= set(tokenize(ev[c]["text"] + " " + ev[c]["title"]))

    def overlap(s: str) -> float:
        toks = set(tokenize(s))
        return len(toks & support_vocab) / len(toks) if toks else 1.0

    unsupported = [s for s in _sentences(answer) if len(set(tokenize(s))) >= 4 and overlap(s) < 0.4]
    checks["pas_d_info_non_sourcee"] = not unsupported

    # Notions clés : un terme rare de la question (IDF élevé, ex. « imprévision ») repris dans la
    # réponse doit figurer dans les preuves ; sinon la réponse affirme la notion sans source.
    missing_terms = sorted((_key_terms(state["question"]) & set(tokenize(answer))) - support_vocab)
    checks["notions_cles_sourcees"] = not missing_terms

    judge = None
    if ctx.llm_check and checks["citations_valides"] and checks["reponse_trouvee"]:
        block = "\n".join(f"[{c}] {ev[c]['text']}" for c in cited)
        try:
            judge, _ = llm_json(JUDGE_SYSTEM, f"PASSAGES :\n{block}\n\nRÉPONSE :\n{answer}",
                                purpose="vérification", usage=ctx.usage, max_tokens=200, model=ctx.model)
        except LLMError as exc:
            ctx.emit("ERROR", "LLM injoignable pendant la vérification", "verify", error=str(exc))
        judge = judge or {}
        raw_claims = [str(x) for x in judge.get("unsupported_claims") or [] if str(x).strip()]
        # Le juge 3B produit des faux négatifs (affirmation présente mot pour mot jugée « non
        # justifiée ») : on ne retient une objection que si l'affirmation est peu présente dans les preuves.
        claims = [c for c in raw_claims if overlap(c) < 0.6 or (_key_terms(c) & set(missing_terms))]
        if judge:
            judge["objections_retenues"] = claims
            judge["objections_ecartees"] = [c for c in raw_claims if c not in claims]
        checks["coherence_reponse_passages"] = bool(judge) and (bool(judge.get("supported")) or not raw_claims
                                                                 or not claims)
    else:
        claims = []
        checks["coherence_reponse_passages"] = checks["citations_valides"] and checks["pas_d_info_non_sourcee"]

    ok = all(checks.values())
    reasons = []
    if not checks["preuves_presentes"]:
        reasons.append("aucune décision du corpus dans les preuves")
    if not checks["reponse_trouvee"]:
        reasons.append("les preuves ne permettent pas de répondre" +
                       (f" ({answer[:160]})" if answer else ""))
    if not checks["citations_valides"]:
        reasons.append("citations absentes ou invalides" + (f" : {invalid}" if invalid else ""))
    if checks["citations_valides"] and not checks["source_corpus_citee"]:
        reasons.append("seule la source externe est citée")
    if not checks.get("affaire_demandee_citee", True):
        reasons.append("la réponse s'appuie sur une autre affaire que celle demandée")
    if unsupported:
        reasons.append(f"{len(unsupported)} phrase(s) peu soutenue(s) par les passages cités")
    if missing_terms:
        reasons.append("notion(s) de la question absente(s) des preuves : " + ", ".join(missing_terms))
    if claims:
        reasons.append("affirmations non justifiées : " + " ; ".join(claims[:2]))
    if judge and judge.get("missing"):
        reasons.append(f"manque : {str(judge['missing'])[:160]}")
    feedback = None if ok else "; ".join(reasons)
    limit = state.get("iteration", 0) >= state.get("max_iterations", MAX_ITERATIONS_DEFAULT)
    verdict = "preuves suffisantes" if ok else ("insuffisant — limite d'étapes atteinte" if limit
                                                 else "insuffisant — nouvelle recherche")
    ctx.emit("CHECK", f"Preuves suffisantes : {'oui' if ok else 'non'}", "verify", ok=ok, checks=checks,
             unsupported_sentences=unsupported, judge=judge, feedback=feedback, next=verdict)
    return {"verification": {"ok": ok, "checks": checks, "feedback": feedback,
                             "unsupported_sentences": unsupported, "judge": judge},
            "feedback": feedback}


def route_verify(state: AgentState) -> str:
    v = state.get("verification") or {}
    if v.get("ok") or state.get("iteration", 0) >= state.get("max_iterations", MAX_ITERATIONS_DEFAULT):
        return "final"
    return "agent"


# ------------------------------------------------------------------ nœud final
def final_node(state: AgentState, config) -> dict:
    ctx = _ctx(config)
    ctx.emit("NODE", "Réponse finale", "final")
    d = state.get("draft") or {}
    v = state.get("verification") or {}
    ev = {e["pid"]: e for e in state.get("evidence", [])}
    if state.get("status") == "erreur LLM":
        status, answer = "erreur", "Le modèle local est injoignable : exécution arrêtée proprement."
    elif not d:
        status = "arrêt"
        answer = "Aucune preuve exploitable n'a été trouvée dans la limite d'étapes : pas de réponse."
    elif v.get("ok"):
        status, answer = "vérifiée", d["answer"]
    else:
        status = "non vérifiée"
        answer = d.get("answer") or "Pas de réponse."
    cited = [c for c in d.get("citations", []) if c in ev]
    sources = {
        "corpus": [{"pid": c, "title": ev[c]["title"], "case_id": ev[c]["case_id"],
                    "section": ev[c]["section_label"], "text": ev[c]["text"]} for c in cited if ev[c]["source"] == "corpus"],
        "external": [{"pid": c, "title": ev[c]["title"], "url": ev[c]["url"], "text": ev[c]["text"]}
                     for c in cited if ev[c]["source"] == "external"],
    }
    stop = {"vérifiée": "réponse finale vérifiée", "non vérifiée": "limite d'étapes atteinte (réponse non vérifiée)",
            "arrêt": "limite d'étapes atteinte sans preuve", "erreur": "erreur contrôlée"}[status]
    final = {"answer": answer, "status": status, "stop_reason": stop, "sources": sources,
             "warning": None if status == "vérifiée" else (v.get("feedback") or stop),
             "iterations": state.get("iteration", 0), "metrics": ctx.metrics()}
    ctx.emit("FINAL", f"Réponse {status}", "final", **final)
    return {"final": final, "status": status}


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("answer", answer_node)
    g.add_node("verify", verify_node)
    g.add_node("final", final_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_agent, {"tools": "tools", "answer": "answer", "final": "final"})
    g.add_edge("tools", "agent")
    g.add_edge("answer", "verify")
    g.add_conditional_edges("verify", route_verify, {"agent": "agent", "final": "final"})
    g.add_edge("final", END)
    return g.compile()


GRAPH = build_graph()
