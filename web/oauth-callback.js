"use strict";

(() => {
  const query = new URLSearchParams(window.location.search);
  window.history.replaceState(null, "", window.location.pathname);
  const state = query.get("state") || "";
  const match = /^([A-Za-z0-9_-]{43})\.([0-9]{1,5})$/.exec(state);
  const issuer = query.get("iss");
  const valid = match && match[0] === state && Number(match[2]) >= 1024 && Number(match[2]) <= 65535
    && issuer === "https://auth.assinafy.com.br" && (query.has("code") !== query.has("error"))
    && Boolean((query.get("code") || query.get("error") || "").trim())
    && [...query.keys()].every(key => query.getAll(key).length === 1);
  if (!valid) {
    document.getElementById("status").textContent = "Retorno inválido. Inicie uma nova conexão no aplicativo.";
    return;
  }
  const callback = new URL(`http://127.0.0.1:${match[2]}/callback`);
  for (const key of ["code", "state", "iss", "error"]) {
    if (query.has(key)) callback.searchParams.set(key, query.get(key));
  }
  window.location.replace(callback.href);
})();
