// Interface jurisprudence (TP2) et agent (TP3). Aucune dépendance : tout passe par l'API /api.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* corps non JSON */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json();
}

const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* stockage indisponible */ } },
};

// ------------------------------------------------------------------ onglets
function showTab(name) {
  $$(".tab[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $("#tab-search").hidden = name !== "search";
  $("#tab-agent").hidden = name !== "agent";
  store.set("tab", name);
}
$$(".tab[data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ------------------------------------------------------------------ statut
async function loadStatus() {
  const el = $("#status");
  try {
    const s = await api("/api/juris/status");
    const dense = s.index && s.index.dense ? "hybride" : "BM25";
    el.className = "status " + (s.llm.ok ? "ok" : "bad");
    $("#status-text").textContent = `${s.model} · ${s.n_cases.toLocaleString("fr-FR")} décisions · index ${dense}`;
    el.title = `Décisions du ${s.date_min} au ${s.date_max} — ${s.index.n_chunks} passages indexés`;
  } catch (e) {
    el.className = "status bad";
    $("#status-text").textContent = "API indisponible";
  }
}

// ------------------------------------------------------------------ utilitaires d'affichage
const SOLUTION_CLASS = (s) => /cassation|annulation/i.test(s || "") ? "bad" : /rejet/i.test(s || "") ? "ok" : "info";

function highlight(text, query) {
  const words = (query || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "")
    .split(/[^a-z0-9]+/).filter((w) => w.length > 3);
  let html = esc(text);
  if (!words.length) return html;
  const stems = [...new Set(words.map((w) => w.slice(0, Math.max(4, w.length - 2))))];
  const re = new RegExp("^(" + stems.map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "i");
  const plain = (t) => t.normalize("NFD").replace(/[̀-ͯ]/g, "");
  return html.replace(/[\wÀ-ÿ]+/g, (w) => (re.test(plain(w)) ? `<mark>${w}</mark>` : w));
}

function citeHtml(text, known, cls = "") {
  // [P1] / [C2] / [E1] -> pastilles cliquables (rouge si la citation ne renvoie à aucun passage fourni)
  return esc(text).replace(/\[((?:[PCE]\d+)(?:\s*,\s*[PCE]\d+)*)\]/g, (_, ids) =>
    ids.split(/\s*,\s*/).map((id) => {
      const ok = !known || known.has(id);
      return `<span class="cite ${id[0] === "E" ? "ext" : ""} ${ok ? "" : "bad"} ${cls}" data-ref="${id}" title="${ok ? "Voir le passage" : "Citation invalide"}">${id}</span>`;
    }).join(""));
}

function flash(container, id) {
  const p = $(`.passage[data-pid="${id}"]`, container);
  if (!p) return;
  const det = p.closest("details");
  if (det) det.open = true;
  p.scrollIntoView({ behavior: "smooth", block: "nearest" });
  p.classList.add("flash");
  setTimeout(() => p.classList.remove("flash"), 1400);
}

function passagesHtml(passages, used = []) {
  const u = new Set(used);
  return `<div class="passages">${passages.map((p) => `
    <div class="passage ${u.has(p.pid) ? "used" : ""} ${p.pid && p.pid[0] === "E" ? "ext" : ""}" data-pid="${esc(p.pid)}">
      <div class="p-head"><b>${esc(p.pid)}</b>${esc(p.title ? p.title + " — " : "")}${esc(p.section_label || p.section || "")}${p.url ? ` — <a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.url)}</a>` : ""}</div>
      ${esc(p.text)}
    </div>`).join("")}</div>`;
}

// ------------------------------------------------------------------ TP2 : recherche
let mode = "hybrid";
let currentCase = null;
let qaHistory = [];
let lastQuery = "";

$$("#mode button").forEach((b) => b.addEventListener("click", () => {
  $$("#mode button").forEach((x) => x.classList.toggle("on", x === b));
  mode = b.dataset.mode;
  if ($("#q").value.trim()) doSearch();
}));

const EXAMPLES = ["licenciement salarié protégé autorisation administrative", "bail rural congé reprise",
  "responsabilité du transporteur aérien retard", "garde à vue assistance avocat nullité", "23-20.161"];
$("#examples").innerHTML = EXAMPLES.map((e) => `<button type="button" class="chip">${esc(e)}</button>`).join("");
$$("#examples .chip").forEach((c) => c.addEventListener("click", () => { $("#q").value = c.textContent; doSearch(); }));

async function loadFilters() {
  try {
    const f = await api("/api/juris/filters");
    $("#f-chamber").innerHTML += f.chambers.map((c) => `<option>${esc(c)}</option>`).join("");
    $("#f-solution").innerHTML += f.solutions.map((c) => `<option>${esc(c)}</option>`).join("");
  } catch (_) { /* filtres facultatifs */ }
}
["#f-chamber", "#f-solution"].forEach((s) => $(s).addEventListener("change", () => $("#q").value.trim() && doSearch()));
$("#search-form").addEventListener("submit", (e) => { e.preventDefault(); doSearch(); });

async function doSearch() {
  const q = $("#q").value.trim();
  if (!q) return;
  lastQuery = q;
  const out = $("#results");
  out.innerHTML = `<p class="loading"><span class="spinner"></span>Recherche…</p>`;
  const params = new URLSearchParams({ q, mode, k: 12 });
  if ($("#f-chamber").value) params.set("chamber", $("#f-chamber").value);
  if ($("#f-solution").value) params.set("solution", $("#f-solution").value);
  try {
    const r = await api("/api/juris/search?" + params);
    $("#results-meta").textContent = `${r.results.length} décisions · ${r.mode} · ${Math.round(r.latency_s * 1000)} ms`;
    if (!r.results.length) { out.innerHTML = `<p class="empty">Aucune décision ne correspond.</p>`; return; }
    out.innerHTML = r.results.map((c) => {
      const p = c.passages[0];
      return `<article class="result" data-id="${c.case_id}">
        <div class="result-title"><span>${esc(c.title)}</span>${c.exact_match ? `<span class="badge gold">n° exact</span>` : `<span class="score">${c.score.toFixed(3)}</span>`}</div>
        <div class="result-meta"><span class="badge ${SOLUTION_CLASS(c.solution)}">${esc(c.solution || "—")}</span> ${esc(c.chamber || "")}</div>
        ${p ? `<div class="snippet"><span class="sec">${esc(p.section_label)}</span>${highlight(p.text.slice(0, 420), q)}</div>` : ""}
      </article>`;
    }).join("");
    $$(".result", out).forEach((el) => el.addEventListener("click", () => openCase(el.dataset.id)));
  } catch (e) {
    out.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------ TP2 : affaire
function showSub(name) {
  $$(".subtab").forEach((b) => b.classList.toggle("active", b.dataset.sub === name));
  ["summary", "qa", "text"].forEach((n) => { $("#sub-" + n).hidden = n !== name; });
}
$$(".subtab").forEach((b) => b.addEventListener("click", () => showSub(b.dataset.sub)));

async function openCase(id) {
  $$(".result").forEach((el) => el.classList.toggle("selected", el.dataset.id === id));
  $("#case-empty").hidden = true;
  $("#case-content").hidden = false;
  $("#case-title").textContent = "Chargement…";
  try {
    const c = await api("/api/juris/case/" + encodeURIComponent(id));
    currentCase = c;
    qaHistory = [];
    $("#case-title").textContent = c.title;
    $("#case-badges").innerHTML = [
      `<span class="badge ${SOLUTION_CLASS(c.solution)}">${esc(c.solution)}</span>`,
      `<span class="badge">${esc(c.chamber)}</span>`,
      c.formation ? `<span class="badge">${esc(c.formation)}</span>` : "",
      c.contested ? `<span class="badge">Décision attaquée : ${esc(c.contested)}</span>` : "",
      c.ecli ? `<span class="badge">${esc(c.ecli)}</span>` : "",
    ].join("");
    $("#case-sommaire").textContent = c.summary || "";
    $("#case-sommaire").hidden = !c.summary;
    $("#case-themes").innerHTML = (c.themes || []).map((t) => `<span class="chip static">${esc(t)}</span>`).join("");
    $("#fulltext").innerHTML = c.sections.map((s) => `<div class="section-block"><h4>${esc(s.label)}</h4><p>${esc(s.text)}</p></div>`).join("");
    $("#summary-out").innerHTML = "";
    $("#chat").innerHTML = `<p class="empty">Interrogez la décision : les réponses s'appuient uniquement sur ses passages.</p>`;
    $("#qa-examples").innerHTML = ["Quels sont les faits ?", "Quelle est la décision de la Cour ?", "Quel moyen était soulevé ?", "Sur quel texte la Cour se fonde-t-elle ?"]
      .map((q) => `<button class="chip" type="button">${q}</button>`).join("");
    $$("#qa-examples .chip").forEach((b) => b.addEventListener("click", () => { $("#ask-q").value = b.textContent; ask(); }));
    showSub("summary");
    loadSummary(false, true);
  } catch (e) {
    $("#case-title").textContent = "Erreur";
    $("#summary-out").innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

const SUM_LABELS = { faits: "Faits", procedure: "Procédure", question: "Question de droit", decision: "Décision", portee: "Portée" };

async function loadSummary(refresh = false, cachedOnly = false) {
  if (!currentCase) return;
  const out = $("#summary-out");
  const btn = $("#btn-summary");
  const id = currentCase.case_id;
  // À l'ouverture, on n'affiche que le résumé déjà en cache (pas d'appel LLM non demandé).
  if (cachedOnly) btn.textContent = "Générer le résumé";
  btn.disabled = true;
  if (!cachedOnly) out.innerHTML = `<p class="loading"><span class="spinner"></span>Le LLM local résume la décision (≈ 1 min sur CPU)…</p>`;
  try {
    const r = await api(`/api/juris/case/${encodeURIComponent(id)}/summary`, { method: "POST", body: JSON.stringify({ refresh, cached_only: cachedOnly }) });
    if (!currentCase || currentCase.case_id !== id) return;
    if (!r.summary) return;
    const known = new Set(r.passages.map((p) => p.pid));
    const ck = r.checks;
    out.innerHTML = `
      <div class="sum-grid">${Object.entries(SUM_LABELS).map(([k, l]) => `
        <div class="sum-item"><h4>${l}</h4><p>${citeHtml(r.summary[k] || "—", known)}</p></div>`).join("")}
      </div>
      <div class="checks">
        <span class="badge ${ck.json_valid ? "ok" : "bad"}">JSON ${ck.json_valid ? "valide" : "invalide"}</span>
        <span class="badge ${ck.invalid_citations.length ? "bad" : "ok"}">${ck.invalid_citations.length ? "Citations invalides : " + ck.invalid_citations.join(", ") : "Citations valides"}</span>
        <span class="badge ${ck.uncited_fields.length ? "warn" : "ok"}">${ck.uncited_fields.length ? "Sans citation : " + ck.uncited_fields.map((f) => SUM_LABELS[f]).join(", ") : "Toutes les rubriques sourcées"}</span>
        ${(ck.auto_cited_fields || []).length ? `<span class="badge info" title="Le modèle n'a pas cité ses sources : le code a rattaché chaque phrase au passage le plus proche.">Citations attribuées par le code : ${ck.auto_cited_fields.map((f) => SUM_LABELS[f]).join(", ")}</span>` : ""}
        <span class="badge">${esc(r.model)} · ${r.cached ? "cache" : r.latency_s + " s"}</span>
      </div>
      <details><summary>Passages fournis au modèle (${r.passages.length})</summary>${passagesHtml(r.passages)}</details>`;
    btn.textContent = "Régénérer";
  } catch (e) {
    out.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  } finally { btn.disabled = false; }
}
$("#btn-summary").addEventListener("click", () => loadSummary($("#btn-summary").textContent === "Régénérer"));
$("#summary-out").addEventListener("click", (e) => { const c = e.target.closest(".cite"); if (c) flash($("#summary-out"), c.dataset.ref); });

$("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask(); });

async function ask() {
  const q = $("#ask-q").value.trim();
  if (!q || !currentCase) return;
  const chat = $("#chat");
  if (chat.querySelector(".empty")) chat.innerHTML = "";
  $("#ask-q").value = "";
  chat.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(q)}</div>`);
  const pending = document.createElement("div");
  pending.className = "msg bot";
  pending.innerHTML = `<span class="spinner"></span>Recherche des passages et rédaction…`;
  chat.appendChild(pending);
  chat.scrollTop = chat.scrollHeight;
  try {
    const r = await api(`/api/juris/case/${encodeURIComponent(currentCase.case_id)}/ask`, { method: "POST", body: JSON.stringify({ question: q, history: qaHistory }) });
    qaHistory.push({ question: q, answer: r.answer });
    const known = new Set(r.passages.map((p) => p.pid));
    pending.innerHTML = `${citeHtml(r.answer, known)}
      <div class="checks">
        ${r.found ? `<span class="badge ${r.citations.length ? "ok" : "warn"}">${r.citations.length ? "Sourcée : " + r.citations.join(", ") : "Aucune citation"}</span>` : `<span class="badge warn">Information absente de la décision</span>`}
        ${r.checks.invalid_citations.length ? `<span class="badge bad">Citations invalides : ${r.checks.invalid_citations.join(", ")}</span>` : ""}
        <span class="badge">${r.latency_s} s</span>
      </div>
      <details><summary>Passages utilisés (${r.passages.length} récupérés)</summary>${passagesHtml(r.passages, r.citations)}</details>`;
    pending.addEventListener("click", (e) => { const c = e.target.closest(".cite"); if (c) flash(pending, c.dataset.ref); });
  } catch (e) {
    pending.innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
  chat.scrollTop = chat.scrollHeight;
}

// ------------------------------------------------------------------ TP3 : agent
const sessionId = store.get("session", null) || (() => { const s = Math.random().toString(36).slice(2, 10); store.set("session", s); return s; })();
let runId = null;
let source = null;

$("#opt-max").addEventListener("input", () => { $("#opt-max-out").textContent = $("#opt-max").value; });

async function loadAgentConfig() {
  try {
    const c = await api("/api/agent/config");
    $("#scenarios").innerHTML = c.scenarios.map((s) => `<button class="scenario" data-q="${esc(s.question)}"><b>${esc(s.label)}</b>${esc(s.question)}</button>`).join("");
    $$(".scenario").forEach((b) => b.addEventListener("click", () => { $("#agent-q").value = b.dataset.q; }));
  } catch (_) { /* config facultative */ }
  refreshMemory();
}

async function refreshMemory() {
  try {
    const m = await api("/api/agent/memory/" + sessionId);
    $("#memory").hidden = !m.case_id;
    if (m.case_id) $("#memory-text").textContent = m.title;
  } catch (_) { $("#memory").hidden = true; }
}
$("#memory-clear").addEventListener("click", async () => { await api("/api/agent/memory/" + sessionId, { method: "DELETE" }); refreshMemory(); });

const TYPE_LABEL = { START: "DÉBUT", DECISION: "DECISION", TOOL: "TOOL", OBSERVATION: "OBSERVATION", GENERATION: "GÉNÉRATION", CHECK: "CHECK", FINAL: "FINAL", ERROR: "ERREUR", APPROVAL: "VALIDATION" };
const CHECK_LABEL = { preuves_presentes: "Preuves présentes", citations_valides: "Citations valides", source_corpus_citee: "Décision du corpus citée", reponse_trouvee: "Réponse trouvée", affaire_demandee_citee: "Affaire demandée citée", notions_cles_sourcees: "Notions clés sourcées", pas_d_info_non_sourcee: "Pas d'info non sourcée", coherence_reponse_passages: "Cohérence réponse / passages" };

function stepDetail(e) {
  const d = e.data || {};
  switch (e.type) {
    case "START":
      return d.memory ? `Mémoire : ${esc(d.memory.title)}` : `Options : ${d.options.max_iterations} étapes max · externe ${d.options.use_external ? "oui" : "non"} · validation ${d.options.require_approval ? "oui" : "non"}`;
    case "DECISION":
      return `${esc(d.reason || "")}${d.action && d.action !== "answer" && d.action !== "stop" ? ` <br><code>${esc(d.action)}(${esc(Object.entries(d.args || {}).filter(([k]) => k !== "case_id" || !d.args.label).map(([k, v]) => k === "label" ? `case_id="${v}"` : `${k}=${JSON.stringify(v)}`).join(", "))})</code>` : ""}${d.guardrail ? `<br><span class="guard">Garde-fou : ${esc(d.guardrail)}</span>` : ""}`;
    case "TOOL":
      return `Exécution de l'outil <code>${esc(d.tool)}</code>`;
    case "OBSERVATION":
      if (d.refused) return "L'utilisateur a refusé l'appel externe.";
      return `${d.results && d.results.length ? `<ul>${d.results.slice(0, 5).map((r) => `<li>${r.label ? `<b>${esc(r.label)}</b> ` : ""}${esc(r.title || r.section || "")}${r.solution ? " — " + esc(r.solution) : ""}${r.text ? " : " + esc(r.text.slice(0, 140)) + "…" : ""}</li>`).join("")}</ul>` : ""}${d.new_evidence && d.new_evidence.length ? `Preuves ajoutées : ${d.new_evidence.map((p) => `<span class="cite ${p[0] === "E" ? "ext" : ""}">${p}</span>`).join("")}` : ""}${d.latency_s != null ? ` · ${d.latency_s} s` : ""}`;
    case "GENERATION":
      return `${citeHtml((d.answer || "").slice(0, 260) + ((d.answer || "").length > 260 ? "…" : ""))}`;
    case "CHECK":
      return `<div class="checklist">${Object.entries(d.checks || {}).map(([k, v]) => `<span class="${v ? "y" : "n"}">${v ? "✓" : "✗"} ${CHECK_LABEL[k] || k}</span>`).join("")}</div>${d.feedback ? `<div style="margin-top:4px">${esc(d.feedback)}</div>` : ""}<div style="margin-top:4px"><b>Suite :</b> ${esc(d.next)}</div>`;
    case "FINAL":
      return `Condition d'arrêt : ${esc(d.stop_reason)}`;
    case "ERROR":
      return `<span style="color:var(--bad)">${esc(d.error)}</span>`;
    case "APPROVAL":
      return `En attente de votre validation pour <code>${esc(d.tool)}</code>`;
    default: return "";
  }
}

function setActiveNode(node) {
  $$(".gnode").forEach((g) => {
    if (g.classList.contains("active")) g.classList.add("visited");
    g.classList.toggle("active", g.dataset.node === node);
  });
}

function addStep(e) {
  const tl = $("#timeline");
  if (e.type === "NODE") { setActiveNode(e.node); return; }
  const n = tl.querySelectorAll(".step").length + 1;
  const li = document.createElement("li");
  li.className = "step";
  const extra = e.type === "CHECK" && e.data.ok ? "ok" : "";
  li.innerHTML = `<div class="step-num">${n}</div><div class="step-body">
    <div class="step-top"><span class="step-type t-${e.type} ${extra}">${TYPE_LABEL[e.type] || e.type}</span>
    <span class="step-title">${esc(e.title)}</span><span class="step-time">${e.t.toFixed(1)} s</span></div>
    <div class="step-detail">${stepDetail(e)}</div></div>`;
  tl.appendChild(li);
  tl.scrollTop = tl.scrollHeight;
  $("#trace-meta").innerHTML = `<span class="spinner"></span>${esc(e.title)}`;
  if (e.type === "APPROVAL") openApproval(e.data);
  if (e.type === "FINAL") renderFinal(e.data);
}

function renderFinal(d) {
  const st = { "vérifiée": "ok", "non vérifiée": "warn", "arrêt": "warn", "erreur": "bad" }[d.status] || "info";
  $("#final-status").innerHTML = `<span class="badge ${st}">${esc(d.status)}</span>`;
  const all = [...(d.sources.corpus || []), ...(d.sources.external || [])];
  const known = new Set(all.map((s) => s.pid));
  $("#final-out").innerHTML = `
    ${d.warning ? `<div class="warning">${esc(d.warning)}</div>` : ""}
    <div class="answer-text">${citeHtml(d.answer, known)}</div>
    <div class="src-group"><h4><span class="pill"></span>Sources du corpus (${d.sources.corpus.length})</h4>
      ${d.sources.corpus.length ? passagesHtml(d.sources.corpus.map((s) => ({ ...s, section_label: s.section }))) : `<p class="muted small">Aucune.</p>`}</div>
    <div class="src-group ext"><h4><span class="pill"></span>Sources externes (${d.sources.external.length})</h4>
      ${d.sources.external.length ? passagesHtml(d.sources.external.map((s) => ({ ...s, section_label: "Source externe" }))) : `<p class="muted small">Aucune.</p>`}</div>`;
  const m = d.metrics || {};
  $("#metrics").innerHTML = [
    [d.iterations, "décisions de l'agent"], [m.llm_calls, "appels LLM"], [m.tool_calls, "appels d'outils"],
    [((m.prompt_tokens || 0) + (m.completion_tokens || 0)).toLocaleString("fr-FR"), "tokens"],
    [m.llm_seconds + " s", "temps LLM"], [m.total_seconds + " s", "durée totale"],
  ].map(([v, l]) => `<div class="metric"><b>${v ?? "—"}</b><span>${l}</span></div>`).join("");
}
$("#final-out").addEventListener("click", (e) => { const c = e.target.closest(".cite"); if (c) flash($("#final-out"), c.dataset.ref); });

function openApproval(d) {
  $("#approval-args").textContent = `${d.tool}(${JSON.stringify(d.args)})`;
  $("#approval").hidden = false;
}
async function answerApproval(ok) {
  $("#approval").hidden = true;
  if (runId) await api(`/api/agent/approve/${runId}`, { method: "POST", body: JSON.stringify({ approve: ok }) });
}
$("#approval-yes").addEventListener("click", () => answerApproval(true));
$("#approval-no").addEventListener("click", () => answerApproval(false));

$("#agent-run").addEventListener("click", runAgent);

async function runAgent() {
  const q = $("#agent-q").value.trim();
  if (!q) return;
  const btn = $("#agent-run");
  btn.disabled = true;
  btn.textContent = "Agent en cours…";
  $("#timeline").innerHTML = "";
  $("#final-out").innerHTML = `<p class="loading"><span class="spinner"></span>L'agent travaille, suivez la trace…</p>`;
  $("#final-status").innerHTML = "";
  $("#metrics").innerHTML = "";
  $$(".gnode").forEach((g) => g.classList.remove("active", "visited"));
  if (source) source.close();
  try {
    const r = await api("/api/agent/run", { method: "POST", body: JSON.stringify({
      question: q, session_id: sessionId, max_iterations: +$("#opt-max").value,
      use_external: $("#opt-external").checked, require_approval: $("#opt-approval").checked,
      llm_check: $("#opt-check").checked, use_memory: $("#opt-memory").checked }) });
    runId = r.run_id;
    let last = -1;
    source = new EventSource(`/api/agent/stream/${runId}`);
    source.addEventListener("trace", (ev) => {
      const e = JSON.parse(ev.data);
      if (e.seq <= last) return; // reconnexion : pas de doublon
      last = e.seq;
      addStep(e);
    });
    source.addEventListener("done", () => {
      source.close();
      btn.disabled = false;
      btn.textContent = "Lancer l'agent";
      $$(".gnode").forEach((g) => { if (g.classList.contains("active")) { g.classList.remove("active"); g.classList.add("visited"); } });
      $("#trace-meta").textContent = `Terminé · run ${runId}`;
      refreshMemory();
    });
    source.onerror = () => { /* EventSource se reconnecte seul */ };
  } catch (e) {
    $("#final-out").innerHTML = `<div class="error">${esc(e.message)}</div>`;
    btn.disabled = false;
    btn.textContent = "Lancer l'agent";
  }
}

// Rejoue la trace d'un run existant (?run=<id>) : utile pour revoir un scénario.
function followRun(id, question) {
  runId = id;
  if (question) $("#agent-q").value = question;
  $("#timeline").innerHTML = "";
  let last = -1;
  source = new EventSource(`/api/agent/stream/${id}`);
  source.addEventListener("trace", (ev) => {
    const e = JSON.parse(ev.data);
    if (e.seq <= last) return;
    last = e.seq;
    if (e.type === "START" && e.data.question) $("#agent-q").value = e.data.question;
    addStep(e);
  });
  source.addEventListener("done", () => { source.close(); $("#trace-meta").textContent = `Terminé · run ${id}`; });
}

// ------------------------------------------------------------------ démarrage
const params = new URLSearchParams(location.search);
showTab(params.get("tab") || store.get("tab", "search"));
loadStatus();
loadFilters();
loadAgentConfig();
// Liens directs : ?q=...&case=... (recherche + affaire), ?run=... (trace de l'agent)
if (params.get("q")) { $("#q").value = params.get("q"); doSearch(); }
if (params.get("case")) openCase(params.get("case")).then(() => {
  const sub = params.get("sub");
  if (sub) showSub(sub);
  if (params.get("summary") === "1") loadSummary(false);
});
if (params.get("run")) { showTab("agent"); followRun(params.get("run")); }
// ?trace=scenario_s2 : relit une trace sauvegardée (docs/traces), étape par étape.
if (params.get("trace")) {
  showTab("agent");
  api("/api/agent/saved/" + encodeURIComponent(params.get("trace"))).then(async (p) => {
    $("#agent-q").value = p.question;
    $("#timeline").innerHTML = "";
    const delay = +(params.get("delay") || 0);
    for (const e of p.events) {
      addStep(e);
      if (delay) await new Promise((r) => setTimeout(r, delay));
    }
    $$(".gnode").forEach((g) => { if (g.classList.contains("active")) { g.classList.remove("active"); g.classList.add("visited"); } });
    $("#trace-meta").textContent = `Trace enregistrée · ${p.started}`;
  }).catch((e) => { $("#final-out").innerHTML = `<div class="error">${esc(e.message)}</div>`; });
}
