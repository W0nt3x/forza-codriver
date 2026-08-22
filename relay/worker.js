// codriver community relay, a Cloudflare Worker.
//
// The codriver app cannot talk to GitHub on a player's behalf (no login), so
// "Share" sends the stage file here, and this worker, holding one token for
// the community repository, opens the pull request. Players need nothing,
// not even a GitHub account. Every pull request is still merged by a human,
// which is the moderation.
//
//   POST /share   body: { "file": "coast-road-sprint.json", "stage": { ...stage json... } }
//                 -> 200 { ok, pr_url, number, updated }
//                 -> 4xx { error } for a bad request, 502 { error } when GitHub refuses
//   GET  /        -> { ok, service, repo }   (a health check, and what you open to test)
//
// Settings (Cloudflare dashboard, Settings -> Variables and Secrets, or wrangler.toml):
//   REPO          "owner/name" of the stages repository, e.g. W0nt3x/codriver-stages
//   BRANCH        base branch, usually "main"
//   GITHUB_TOKEN  secret. A fine-grained token limited to that one repository with
//                 Contents: read and write, Pull requests: read and write.
//   SHARE_SECRET  secret, optional. When set, a share must carry the same value in
//                 the X-Codriver-Secret header (the app sends community.relay_secret).
//                 Keeps scanners and strangers off the relay; the app is open source,
//                 so it is a filter, not a lock. Merging by hand remains the lock.

const SAFE_FILE = /^[a-z0-9][a-z0-9\-]{0,80}\.json$/;
const MAX_BYTES = 3 * 1024 * 1024;
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-allow-headers": "content-type",
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    const url = new URL(request.url);
    if (request.method === "GET") {
      return json({
        ok: true, service: "codriver-relay", repo: env.REPO || null,
        configured: Boolean(env.GITHUB_TOKEN && env.REPO), secret_required: Boolean(env.SHARE_SECRET),
      });
    }
    if (request.method !== "POST" || url.pathname !== "/share") {
      return json({ error: "use POST /share" }, 404);
    }
    if (!env.GITHUB_TOKEN || !env.REPO) {
      return json({ error: "relay not configured: set REPO and the GITHUB_TOKEN secret" }, 500);
    }
    // Both sides trimmed: a secret pasted into a dashboard often brings a
    // line break along, and that must not turn into a silent 403.
    const wanted = (env.SHARE_SECRET || "").trim();
    if (wanted && (request.headers.get("x-codriver-secret") || "").trim() !== wanted) {
      return json({ error: "relay refused the share: wrong or missing secret (community.relay_secret)" }, 403);
    }
    const declared = Number(request.headers.get("content-length") || 0);
    if (declared > MAX_BYTES) return json({ error: "stage file too large" }, 413);

    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "body must be JSON: { file, stage }" }, 400);
    }
    const problem = validate(body);
    if (problem) return json({ error: problem }, 400);

    const { file, stage } = body;
    const content = JSON.stringify(stage, null, 1) + "\n";
    if (content.length > MAX_BYTES) return json({ error: "stage file too large" }, 413);

    try {
      const result = await openPullRequest(env, file, stage, content);
      return json({ ok: true, ...result });
    } catch (/** @type {any} */ e) {
      return json({ error: `github: ${e.message}` }, e.status === 401 || e.status === 403 ? 500 : 502);
    }
  },
};

export function validate(body) {
  if (!body || typeof body !== "object") return "body must be an object";
  const { file, stage } = body;
  if (typeof file !== "string" || !SAFE_FILE.test(file)) return "file must look like coast-road-sprint.json";
  if (!stage || typeof stage !== "object") return "stage must be the stage JSON";
  if (stage.format !== "codriver-stage") return "not a codriver stage file";
  if (typeof stage.name !== "string" || !stage.name.trim()) return "stage has no name";
  if (!Array.isArray(stage.notes) || stage.notes.length < 1) return "stage has no notes";
  if (stage.notes.length > 2000) return "too many notes to be a stage";
  if (!Array.isArray(stage.line) || stage.line.length < 10) return "stage has no line";
  if (stage.line.length > 60000) return "line too long to be a stage";
  return null;
}

async function openPullRequest(env, file, stage, content) {
  const gh = github(env);
  const base = env.BRANCH || "main";
  const stem = file.slice(0, -5);

  const ref = await gh(`git/ref/heads/${base}`);
  const baseSha = ref.object.sha;

  // One branch per share, named so two shares of the same stage do not collide.
  const head = `share/${stem}-${Date.now().toString(36)}`;
  await gh("git/refs", { method: "POST", body: { ref: `refs/heads/${head}`, sha: baseSha } });

  // Updating an existing file needs its blob sha; a new file must not send one.
  let existingSha = null;
  try {
    const existing = await gh(`contents/stages/${file}?ref=${encodeURIComponent(base)}`);
    existingSha = existing.sha || null;
  } catch (/** @type {any} */ e) {
    if (e.status !== 404) throw e;
  }

  const verb = existingSha ? "Update" : "Add";
  await gh(`contents/stages/${file}`, {
    method: "PUT",
    body: {
      message: `${verb} stage ${stem}`,
      content: toBase64(new TextEncoder().encode(content)),
      branch: head,
      ...(existingSha ? { sha: existingSha } : {}),
    },
  });

  const community = stage.community || {};
  const race = community.race || stage.name;
  const author = (community.author || "").trim() || "anonymous";
  const pr = await gh("pulls", {
    method: "POST",
    body: {
      title: `${verb} stage: ${race}`,
      head,
      base,
      body: [
        `Shared from the codriver app by **${author}**.`,
        "",
        `- race: ${race}`,
        `- length: ${((stage.length_m || 0) / 1000).toFixed(2)} km`,
        `- notes: ${stage.notes.length}`,
        `- tool version: ${community.tool_version || "unknown"}`,
        "",
        "Merging publishes it in the Community list of every codriver install.",
      ].join("\n"),
    },
  });
  return { pr_url: pr.html_url, number: pr.number, updated: Boolean(existingSha) };
}

/**
 * @typedef {{ method?: string, body?: any }} GhOptions
 * @typedef {Error & { status?: number }} GhError
 */
function github(env) {
  /** @param {string} path @param {GhOptions} [opts] */
  return async (path, opts = {}) => {
    const { method = "GET", body } = opts;
    const r = await fetch(`https://api.github.com/repos/${env.REPO}/${path}`, {
      method,
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "user-agent": "codriver-relay",
        "x-github-api-version": "2022-11-28",
        ...(body ? { "content-type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await r.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { raw: text };
    }
    if (!r.ok) {
      /** @type {GhError} */
      const e = Object.assign(new Error(`${method} ${path} -> ${r.status} ${data.message || text}`), { status: r.status });
      throw e;
    }
    return data;
  };
}

function toBase64(bytes) {
  // btoa wants a binary string; build it in chunks, a spread of 200k bytes
  // would blow the argument limit.
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...CORS },
  });
}
