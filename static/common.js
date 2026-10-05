"use strict";

// Shared helpers. All DOM writes use textContent: server and model output is never parsed as HTML.
const api = {
  csrf: null,

  async request(path, { method = "GET", body } = {}) {
    const headers = {};
    if (method !== "GET") headers["X-CSRF-Token"] = api.csrf || "";
    if (body && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const res = await fetch(path, { method, body, headers, credentials: "same-origin" });
    if (res.status === 204) return null;
    const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
    if (!res.ok) {
      const err = new Error(typeof data?.detail === "string" ? data.detail : `Request failed (${res.status})`);
      err.status = res.status;
      throw err;
    }
    return data;
  },

  async me() {
    try {
      const me = await api.request("/api/me");
      api.csrf = me.csrf_token;
      return me;
    } catch (e) {
      if (e.status === 401) return null;
      throw e;
    }
  },

  async logout() {
    await api.request("/auth/logout", { method: "POST" });
    location.href = "/";
  },
};

function $(id) { return document.getElementById(id); }

function setList(ul, items) {
  ul.replaceChildren(...items.map((t) => {
    const li = document.createElement("li");
    li.textContent = t;
    return li;
  }));
}
