// Interface : ne parle qu'au backend (/api/...), jamais directement au modèle.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pct = (x) => (x == null ? "–" : (100 * x).toFixed(1) + " %");

let strategies = {};

async function api(path, body) {
  const res = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : undefined);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : res.statusText);
  return data;
}

async function init() {
  const cfg = await api("/api/config");
  strategies = cfg.strategies;
  for (const [name] of Object.entries(strategies)) {
    $("strategy").add(new Option(name, name, false, name === "few_shot"));
  }
  $("strategy-desc").textContent = strategies[$("strategy").value];

  try {
    const examples = await api("/api/examples");
    for (const ex of examples) {
      $("example").add(new Option(`${ex.id} — ${ex.title} (${ex.n_sents} phrases)`, ex.id));
    }
  } catch (e) {
    $("example").add(new Option("(données absentes : python -m scripts.prepare_data)", ""));
  }

  api("/api/health").then((h) => {
    $("health").textContent = `${h.model} @ ${h.provider} : ${h.ok ? "OK" : "indisponible"}`;
    $("health").className = "badge " + (h.ok ? "ok" : "bad");
    if (!h.ok) $("health").title = h.error || `modèle absent, disponibles : ${h.models.join(", ")}`;
  });
}

async function loadExample() {
  const id = $("example").value;
  if (!id) return;
  const q = new URLSearchParams({ relations: $("relations").value });
  if ($("max_sents").value) q.set("max_sents", $("max_sents").value);
  const ex = await api(`/api/examples/${id}?${q}`);
  $("text").value = ex.text;
}

function buildRequest() {
  const num = (id) => Number($(id).value);
  const req = {
    strategy: $("strategy").value,
    relations: $("relations").value,
    n_shots: num("n_shots"),
    drop_invalid: $("drop_invalid").checked,
    params: {
      temperature: num("temperature"), seed: num("seed"), top_p: num("top_p"),
      top_k: num("top_k"), max_tokens: num("max_tokens"), json_mode: $("json_mode").checked,
    },
  };
  if ($("example").value) {
    req.example_id = $("example").value;
    if ($("max_sents").value) req.max_sents = num("max_sents");
  } else {
    req.text = $("text").value;
  }
  return req;
}

function relationTable(rels, { showProblems = false, rejected = false } = {}) {
  if (!rels.length) return '<p class="muted">Aucune relation.</p>';
  const hasGold = rels.some((r) => "correct" in r);
  const rows = rels.map((r) => {
    const cls = [rejected ? "rejected" : "", hasGold ? (r.correct ? "correct" : "wrong") : ""].join(" ");
    const rawRel = r.raw_relation && r.raw_relation !== r.relation
      ? ` <span class="muted">(${esc(r.raw_relation)})</span>` : "";
    return `<tr class="${cls}">
      <td>${esc(r.subject)}${r.subject_grounded === false ? " ⚠" : ""}</td>
      <td><code>${esc(r.relation)}</code>${rawRel}</td>
      <td>${esc(r.object)}${r.object_grounded === false ? " ⚠" : ""}</td>
      ${hasGold ? `<td>${r.correct ? "✔" : "✘"}</td>` : ""}
      ${showProblems ? `<td>${esc((r.problems || []).join(", "))}</td>` : ""}
    </tr>`;
  }).join("");
  return `<div class="table-wrap"><table><thead><tr><th>Sujet</th><th>Relation</th><th>Objet</th>
    ${hasGold ? "<th>Réf.</th>" : ""}${showProblems ? "<th>Motif du rejet</th>" : ""}</tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function badges(r) {
  const b = (ok, label) => `<span class="badge ${ok ? "ok" : "bad"}">${label}</span>`;
  let html = b(r.json_valid, `JSON ${r.json_valid ? "valide" : "invalide"}`)
    + b(r.schema_valid, `schéma ${r.schema_valid ? "respecté" : "non respecté"}`)
    + `<span class="badge">${r.relations.length} retenue(s) / ${r.rejected.length} rejetée(s)</span>`
    + `<span class="badge">${r.latency_s.toFixed(2)} s</span>`;
  if (r.score) {
    html += `<span class="badge">P ${pct(r.score.precision)}</span>`
      + `<span class="badge">R ${pct(r.score.recall)}</span>`
      + `<span class="badge">F1 ${pct(r.score.f1)}</span>`
      + `<span class="badge">hallucinations ${pct(r.score.hallucination_rate)}</span>`;
  }
  return html;
}

function renderResult(r) {
  let html = `<div>${badges(r)}</div>`;
  if (r.errors.length) html += `<p class="error">${r.errors.map(esc).join("<br>")}</p>`;
  html += `<h3>Relations extraites</h3>${relationTable(r.relations)}`;
  if (r.rejected.length) {
    html += `<h3>Relations rejetées par la validation</h3>${relationTable(r.rejected, { showProblems: true, rejected: true })}`;
  }
  if (r.gold) {
    html += `<details><summary>Référence Re-DocRED (${r.gold.length} relations)</summary>${relationTable(r.gold)}</details>`;
  }
  if (r.reasoning) {
    html += `<details open><summary>Raisonnement du modèle</summary><pre>${esc(
      Array.isArray(r.reasoning) ? r.reasoning.join("\n") : r.reasoning)}</pre></details>`;
  }
  if (r.entities) {
    html += `<details><summary>Entités (étape 1 de la décomposition)</summary><pre>${esc(
      r.entities.map((e) => `${e.name} (${e.type})`).join("\n"))}</pre></details>`;
  }
  const out = {
    relations: r.relations.map(({ subject, relation, object }) => ({ subject, relation, object })),
  };
  html += `<details><summary>JSON final (validé)</summary><pre>${esc(JSON.stringify(out, null, 2))}</pre></details>`;
  for (const c of r.calls) {
    html += `<details><summary>Appel « ${esc(c.step)} » : sortie brute du modèle</summary><pre>${esc(c.output)}</pre></details>`;
    html += `<details><summary>Appel « ${esc(c.step)} » : prompt envoyé (${c.messages.length} messages)</summary><pre>${esc(
      c.messages.map((m) => `### ${m.role}\n${m.content}`).join("\n\n"))}</pre></details>`;
  }
  $("result").innerHTML = html;
}

function renderComparison(rows, variable) {
  const hasScore = rows.some((r) => r.score);
  const body = rows.map((r) => `<tr>
    <td>${esc(variable === "temperature" ? r.params.temperature : r.strategy)}</td>
    <td>${r.json_valid ? "✔" : "✘"}</td><td>${r.schema_valid ? "✔" : "✘"}</td>
    <td class="num">${r.relations.length}</td><td class="num">${r.rejected.length}</td>
    ${hasScore ? `<td class="num">${pct(r.score.precision)}</td><td class="num">${pct(r.score.recall)}</td>
      <td class="num">${pct(r.score.f1)}</td><td class="num">${pct(r.score.hallucination_rate)}</td>` : ""}
    <td class="num">${r.latency_s.toFixed(2)} s</td></tr>`).join("");
  let html = `<div class="table-wrap"><table><thead><tr>
    <th>${variable === "temperature" ? "Température" : "Stratégie"}</th><th>JSON</th><th>Schéma</th>
    <th>Retenues</th><th>Rejetées</th>
    ${hasScore ? "<th>P</th><th>R</th><th>F1</th><th>Halluc.</th>" : ""}<th>Latence</th>
    </tr></thead><tbody>${body}</tbody></table></div>`;
  if (!hasScore) html += '<p class="muted">Sélectionnez un exemple du jeu d\'évaluation pour obtenir P/R/F1.</p>';
  html += '<p class="muted">Un seul document : pour conclure, utilisez <code>python -m scripts.evaluate</code> sur plusieurs documents.</p>';
  for (const r of rows) {
    const label = variable === "temperature" ? `T = ${r.params.temperature}` : r.strategy;
    html += `<details><summary>${esc(label)}</summary>${relationTable(r.relations)}</details>`;
  }
  $("result").innerHTML = html;
}

async function run(kind) {
  const buttons = document.querySelectorAll("button");
  buttons.forEach((b) => (b.disabled = true));
  $("status").textContent = "Extraction en cours…";
  $("status").className = "muted";
  const t0 = performance.now();
  try {
    const req = buildRequest();
    if (kind === "single") {
      renderResult(await api("/api/extract", req));
    } else if (kind === "strategies") {
      req.strategies = Object.keys(strategies);
      req.temperatures = [req.params.temperature];
      renderComparison(await api("/api/compare", req), "strategy");
    } else {
      req.strategies = [req.strategy];
      req.temperatures = [0, 0.3, 0.7, 1.0];
      renderComparison(await api("/api/compare", req), "temperature");
    }
    $("status").textContent = `Terminé en ${((performance.now() - t0) / 1000).toFixed(1)} s.`;
  } catch (e) {
    $("status").textContent = "Erreur : " + e.message;
    $("status").className = "error";
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

$("temperature").addEventListener("input", () => ($("temperature-out").value = $("temperature").value));
$("strategy").addEventListener("change", () => ($("strategy-desc").textContent = strategies[$("strategy").value]));
$("example").addEventListener("change", loadExample);
$("relations").addEventListener("change", loadExample);
$("max_sents").addEventListener("change", loadExample);
$("text").addEventListener("input", () => ($("example").value = ""));
$("run").addEventListener("click", () => run("single"));
$("cmp-strat").addEventListener("click", () => run("strategies"));
$("cmp-temp").addEventListener("click", () => run("temperatures"));
init();
