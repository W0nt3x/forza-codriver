// Exercises relay/worker.js without Cloudflare or GitHub: the global fetch is
// replaced by a fake GitHub that records every call. Run by
// tests/test_relay_worker.py when node is installed. Exits non-zero on the
// first failed assertion.
import assert from "node:assert/strict";
import worker, { validate } from "../relay/worker.js";

const calls = [];
let existingFile = false;
globalThis.fetch = async (url, init = {}) => {
  const method = init.method || "GET";
  const path = url.replace("https://api.github.com/repos/W0nt3x/codriver-stages/", "");
  calls.push({ method, path, body: init.body ? JSON.parse(init.body) : null, headers: init.headers });
  const reply = (status, data) => new Response(JSON.stringify(data), { status });
  if (method === "GET" && path === "git/ref/heads/main") return reply(200, { object: { sha: "base123" } });
  if (method === "POST" && path === "git/refs") return reply(201, { ref: init.body && JSON.parse(init.body).ref });
  if (method === "GET" && path.startsWith("contents/stages/")) return existingFile ? reply(200, { sha: "old456" }) : reply(404, { message: "Not Found" });
  if (method === "PUT" && path.startsWith("contents/stages/")) return reply(201, { content: { path } });
  if (method === "POST" && path === "pulls") return reply(201, { html_url: "https://github.com/W0nt3x/codriver-stages/pull/7", number: 7 });
  return reply(500, { message: `unexpected ${method} ${path}` });
};

const env = { GITHUB_TOKEN: "tok", REPO: "W0nt3x/codriver-stages", BRANCH: "main" };
const stage = {
  format: "codriver-stage", version: 1, name: "coast-road-sprint", length_m: 4200,
  notes: [{ at_m: 30, tokens: ["3", "right"] }],
  line: Array.from({ length: 20 }, (_, i) => [i * 3, 0, 0]),
  community: { race: "coast-road-sprint", author: "nils", tool_version: "0.2.0" },
};
const post = (body, headers = {}) => worker.fetch(new Request("https://relay.example/share", {
  method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify(body),
}), env);

// validation
assert.equal(validate({ file: "coast-road-sprint.json", stage }), null);
assert.match(validate({ file: "../evil.json", stage }), /file must/);
assert.match(validate({ file: "x.json", stage: { ...stage, notes: [] } }), /no notes/);
assert.match(validate({ file: "x.json", stage: { ...stage, format: "gpx" } }), /not a codriver stage/);

// health
const health = await (await worker.fetch(new Request("https://relay.example/"), env)).json();
assert.deepEqual(health, { ok: true, service: "codriver-relay", repo: env.REPO, configured: true, secret_required: false });

// an unconfigured relay says so instead of touching GitHub
const unconf = await worker.fetch(new Request("https://relay.example/share", { method: "POST", body: "{}" }), { REPO: env.REPO });
assert.equal(unconf.status, 500);

// a good share: branch from base, file written on the branch, PR opened
calls.length = 0;
let r = await post({ file: "coast-road-sprint.json", stage });
let data = await r.json();
assert.equal(r.status, 200, JSON.stringify(data));
assert.equal(data.ok, true);
assert.equal(data.pr_url, "https://github.com/W0nt3x/codriver-stages/pull/7");
assert.equal(data.updated, false);
assert.deepEqual(calls.map((c) => `${c.method} ${c.path.split("?")[0]}`), [
  "GET git/ref/heads/main",
  "POST git/refs",
  "GET contents/stages/coast-road-sprint.json",
  "PUT contents/stages/coast-road-sprint.json",
  "POST pulls",
]);
const refCall = calls[1], putCall = calls[3], prCall = calls[4];
assert.equal(refCall.body.sha, "base123");
assert.match(refCall.body.ref, /^refs\/heads\/share\/coast-road-sprint-/);
assert.equal(putCall.body.branch, refCall.body.ref.replace("refs/heads/", ""));
assert.equal(putCall.body.sha, undefined, "a new file sends no sha");
const written = JSON.parse(Buffer.from(putCall.body.content, "base64").toString("utf8"));
assert.equal(written.name, "coast-road-sprint");
assert.equal(prCall.body.title, "Add stage: coast-road-sprint");
assert.equal(prCall.body.base, "main");
assert.match(prCall.body.body, /nils/);
assert.equal(calls[0].headers.authorization, "Bearer tok");

// sharing a stage that already exists becomes an update, with the old sha
existingFile = true;
calls.length = 0;
r = await post({ file: "coast-road-sprint.json", stage });
data = await r.json();
assert.equal(data.updated, true);
assert.equal(calls[3].body.sha, "old456");
assert.equal(calls[4].body.title, "Update stage: coast-road-sprint");

// garbage is refused before GitHub is involved
calls.length = 0;
r = await post({ file: "coast-road-sprint.json", stage: { hello: 1 } });
assert.equal(r.status, 400);
assert.equal(calls.length, 0);
r = await post({ file: "coast-road-sprint.json", stage }, { "content-length": String(10 * 1024 * 1024) });
assert.equal(r.status, 413);

// with SHARE_SECRET set, the header has to match; nothing reaches GitHub otherwise
calls.length = 0;
const envSecret = { ...env, SHARE_SECRET: "s3cret" };
const postSecret = (headers) => worker.fetch(new Request("https://relay.example/share", {
  method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify({ file: "coast-road-sprint.json", stage }),
}), envSecret);
r = await postSecret({});
assert.equal(r.status, 403);
r = await postSecret({ "x-codriver-secret": "wrong" });
assert.equal(r.status, 403);
assert.equal(calls.length, 0, "refused before GitHub");
r = await postSecret({ "x-codriver-secret": "s3cret" });
assert.equal(r.status, 200);
const healthSecret = await (await worker.fetch(new Request("https://relay.example/"), envSecret)).json();
assert.equal(healthSecret.secret_required, true);

// GitHub refusing the token is reported, not swallowed
globalThis.fetch = async () => new Response(JSON.stringify({ message: "Bad credentials" }), { status: 401 });
r = await post({ file: "coast-road-sprint.json", stage });
data = await r.json();
assert.equal(r.status, 500);
assert.match(data.error, /Bad credentials/);

console.log("relay worker check: ok");
