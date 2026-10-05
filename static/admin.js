"use strict";

function refCard(ref) {
  const li = document.createElement("li");
  li.className = "ref";

  const img = document.createElement("img");
  img.src = `/api/references/${encodeURIComponent(ref.id)}/image`;
  img.alt = `${ref.condition} tyre, ${ref.tread_depth_mm} mm`;
  img.loading = "lazy";

  const title = document.createElement("strong");
  title.textContent = `${ref.condition.replace("_", " ")} · ${ref.tread_depth_mm} mm`;

  const meta = document.createElement("p");
  meta.className = "muted";
  meta.textContent = [
    ref.km_driven != null ? `${ref.km_driven.toLocaleString()} km` : null,
    ref.tyre_size, ref.brand_model, ref.notes,
  ].filter(Boolean).join(" · ");

  const del = document.createElement("button");
  del.type = "button";
  del.className = "link danger";
  del.textContent = "Delete";
  del.addEventListener("click", async () => {
    if (!confirm("Delete this reference photo?")) return;
    await api.request(`/api/references/${encodeURIComponent(ref.id)}`, { method: "DELETE" });
    load();
  });

  li.append(img, title, meta, del);
  return li;
}

async function load() {
  const { items } = await api.request("/api/references");
  $("count").textContent = items.length;
  $("refs").replaceChildren(...items.map(refCard));
}

async function onAdd(ev) {
  ev.preventDefault();
  const form = ev.target;
  const data = new FormData(form);
  for (const [k, v] of [...data.entries()]) if (v === "") data.delete(k);
  const status = $("add-status");
  status.textContent = "Uploading…";
  try {
    await api.request("/api/references", { method: "POST", body: data });
    form.reset();
    status.textContent = "Added.";
    load();
  } catch (e) {
    status.textContent = e.message;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  const me = await api.me();
  if (!me || !me.is_admin) { $("denied").hidden = false; return; }
  $("admin").hidden = false;
  $("logout").addEventListener("click", api.logout);
  $("add").addEventListener("submit", onAdd);
  load();
});
