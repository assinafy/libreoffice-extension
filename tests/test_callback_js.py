import subprocess
from pathlib import Path


def test_https_callback_relay():
    source = Path("web/oauth-callback.js").read_text()
    script = r"""
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = process.argv[1];
const state = "a".repeat(43) + ".54321";
function run(query) {
  const events = [];
  const label = {};
  vm.runInNewContext(source, {
    URL, URLSearchParams,
    window: { location: { search: query, pathname: "/libreoffice/oauth-callback",
      replace: url => events.push(["navigate", url]) },
      history: {replaceState: () => events.push(["clear"])} },
    document: {getElementById: () => label},
  });
  return events;
}
const valid = new URLSearchParams({state, code: "one-time-code",
  iss: "https://auth.assinafy.com.br"});
const events = run("?" + valid);
assert.equal(events[0][0], "clear");
assert.equal(events[1][0], "navigate");
const target = new URL(events[1][1]);
assert.equal(target.origin, "http://127.0.0.1:54321");
assert.equal(target.searchParams.get("code"), "one-time-code");
assert.equal(target.searchParams.get("iss"), "https://auth.assinafy.com.br");
assert.equal(target.searchParams.get("state"), state);
const declined = new URLSearchParams(valid);
declined.delete("code"); declined.set("error", "access_denied");
declined.set("error_description", "private details");
const deniedTarget = new URL(run("?" + declined)[1][1]);
assert.equal(deniedTarget.searchParams.get("error"), "access_denied");
assert.equal(deniedTarget.searchParams.has("error_description"), false);
assert.equal(deniedTarget.searchParams.has("code"), false);
for (const [key,value] of [["state","bad.54321"], ["state","a".repeat(43)+".22"],
  ["iss","https://other.invalid"]]) {
  const invalid = new URLSearchParams(valid); invalid.set(key,value);
  assert.deepEqual(run("?" + invalid), [["clear"]]);
}
const noIssuer = new URLSearchParams(valid); noIssuer.delete("iss");
assert.deepEqual(run("?" + noIssuer), [["clear"]]);
assert.deepEqual(run("?" + valid + "&state=other"), [["clear"]]);
assert.deepEqual(run("?" + valid + "&error=access_denied"), [["clear"]]);
for (const suffix of ["\n", "\r", "\r\n"]) {
  const invalid = new URLSearchParams(valid); invalid.set("state", state + suffix);
  assert.deepEqual(run("?" + invalid), [["clear"]]);
}
for (const key of ["code", "error"]) {
  for (const value of ["", " ", "\t\r\n"]) {
    const invalid = new URLSearchParams(valid);
    invalid.delete("code"); invalid.set(key, value);
    assert.deepEqual(run("?" + invalid), [["clear"]]);
  }
}

"""
    subprocess.run(["node", "-e", script, source], check=True, capture_output=True, text=True)
