"use strict";

const lingxi = (() => {
  let csrf = "";
  let unauthorizedHandler = () => {};

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
      options.body = JSON.stringify(options.body);
    }
    if (csrf && options.method && options.method !== "GET") {
      headers.set("X-Lingxi-CSRF", csrf);
    }
    const response = await fetch(path, { credentials: "same-origin", ...options, headers });
    const value = await response.json();
    if (!response.ok) {
      if (response.status === 401) {
        csrf = "";
        unauthorizedHandler();
      }
      const error = new Error(value.error?.message || "请求失败");
      error.code = value.error?.code || "request_failed";
      error.status = response.status;
      throw error;
    }
    return value;
  }

  async function session() {
    const value = await request("/api/session");
    csrf = value.csrf;
    return value;
  }

  async function login(password) {
    const value = await request("/api/login", { method: "POST", body: { password } });
    csrf = value.csrf;
    return value;
  }

  function setUnauthorizedHandler(handler) {
    unauthorizedHandler = typeof handler === "function" ? handler : () => {};
  }

  function text(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value == null ? "" : String(value);
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  return { request, session, login, setUnauthorizedHandler, text, clear };
})();
