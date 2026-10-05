"use strict";

const VERDICT_LABEL = {
  ok: "Good condition",
  monitor: "Keep an eye on it",
  replace_soon: "Replace soon",
  replace_now: "Replace now – do not drive",
  unable_to_assess: "Couldn't assess the photo",
};

let currentAnalysis = null;

function addMessage(role, text) {
  const li = document.createElement("li");
  li.className = role;
  li.textContent = text;
  $("messages").append(li);
  li.scrollIntoView({ block: "nearest" });
}

function km(n) { return `${n.toLocaleString()} km`; }

function showResult(r) {
  const e = r.estimate;
  const v = $("verdict");
  v.textContent = VERDICT_LABEL[e.verdict] || e.verdict;
  v.dataset.verdict = e.verdict;
  $("summary").textContent = r.summary;
  $("s-depth").textContent = e.verdict === "unable_to_assess" ? "–" : `${e.min_tread_mm} mm`;
  $("s-km").textContent = e.verdict === "unable_to_assess" ? "–"
    : `${km(e.km_to_legal_limit)} (${km(e.km_to_legal_limit_low)} – ${km(e.km_to_legal_limit_high)})`;
  $("s-rec").textContent = e.verdict === "unable_to_assess" ? "–" : km(e.km_to_recommended_limit);
  $("s-conf").textContent = r.assessment.confidence;
  setList($("warnings"), e.warnings);
  setList($("advice"), e.advice);
  $("observations").textContent = r.assessment.observations;
  $("disclaimer").textContent = r.disclaimer;
  $("result").hidden = false;

  $("messages").replaceChildren();
  $("chat").hidden = e.verdict === "unable_to_assess";
  if (r.reply) {
    addMessage("user", new FormData($("upload")).get("question"));
    addMessage("assistant", r.reply);
  }
}

async function onUpload(ev) {
  ev.preventDefault();
  const form = ev.target;
  const button = form.querySelector("button");
  const status = $("upload-status");
  const data = new FormData(form);
  for (const k of ["km_since_fitted", "question"]) if (!data.get(k)) data.delete(k);

  button.disabled = true;
  status.textContent = "Analysing your tyre… this can take up to a minute.";
  try {
    const r = await api.request("/api/analyses", { method: "POST", body: data });
    currentAnalysis = r.analysis_id;
    status.textContent = "";
    showResult(r);
  } catch (e) {
    status.textContent = e.message;
  } finally {
    button.disabled = false;
  }
}

async function onAsk(ev) {
  ev.preventDefault();
  const input = ev.target.elements.message;
  const text = input.value.trim();
  if (!text || !currentAnalysis) return;
  input.value = "";
  addMessage("user", text);
  const button = ev.target.querySelector("button");
  button.disabled = true;
  try {
    const r = await api.request(`/api/analyses/${encodeURIComponent(currentAnalysis)}/messages`,
      { method: "POST", body: { message: text } });
    addMessage("assistant", r.reply);
  } catch (e) {
    addMessage("error", e.message);
  } finally {
    button.disabled = false;
    input.focus();
  }
}

function onPickFile(ev) {
  const file = ev.target.files[0];
  const img = $("preview");
  if (img.src) URL.revokeObjectURL(img.src);
  if (!file) { img.hidden = true; return; }
  img.src = URL.createObjectURL(file);
  img.hidden = false;
}

document.addEventListener("DOMContentLoaded", async () => {
  const me = await api.me();
  if (!me) { $("signin").hidden = false; return; }
  $("who").textContent = me.name;
  $("admin-link").hidden = !me.is_admin;
  $("nav").hidden = false;
  $("app").hidden = false;
  $("logout").addEventListener("click", api.logout);
  $("upload").addEventListener("submit", onUpload);
  $("upload").elements.image.addEventListener("change", onPickFile);
  $("ask").addEventListener("submit", onAsk);
});
