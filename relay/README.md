# Community relay

The small piece that makes **Share** a single click. The codriver app has no
GitHub login, so instead of asking every player to upload a file by hand, the
app sends the stage to this relay, and the relay, which holds one token for
the stages repository, opens the pull request. Players need nothing, not
even a GitHub account. Every pull request is still merged by a human; that
is the moderation, and it stays that way.

It is a Cloudflare Worker (free tier is plenty: a share is one request),
about 150 lines in [worker.js](worker.js). You only need this if you run the
community repository. Players never touch it.

## Set it up once (about ten minutes)

**1. A GitHub token that can only touch the stages repo.**
GitHub → Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token.

- Name: `codriver relay`. Expiration: a year (put a reminder in your calendar;
  when it expires, Share quietly falls back to the manual upload page).
- Repository access: *Only select repositories* → `codriver-stages`.
- Repository permissions: **Contents: Read and write**, **Pull requests: Read
  and write**. Metadata is added automatically. Nothing else.
- Generate, copy the token. You will not see it again.

**2. The worker.** Two ways; the dashboard one needs no tools.

*Dashboard:* <https://dash.cloudflare.com> → Workers & Pages → Create →
Start with Hello World → name it `codriver-relay` → Deploy. Then *Edit code*,
replace everything with the contents of `worker.js`, *Deploy*. Then
Settings → Variables and Secrets → add:

| name | type | value |
|---|---|---|
| `REPO` | text | `W0nt3x/codriver-stages` |
| `BRANCH` | text | `main` |
| `GITHUB_TOKEN` | **secret** | the token from step 1 |

*Or wrangler, from this folder:* `npx wrangler login`, `npx wrangler deploy`,
`npx wrangler secret put GITHUB_TOKEN`. `REPO` and `BRANCH` come from
`wrangler.toml`.

**3. Check it.** Open the worker URL (looks like
`https://codriver-relay.<your-account>.workers.dev`) in a browser. It should
answer `{"ok":true,"service":"codriver-relay","repo":"W0nt3x/codriver-stages","configured":true}`.
If `configured` is false, a variable is missing.

**4. Tell the app.** Put that URL into `community.relay_url` in
`config/defaults.yaml` (so every install gets it) and push. Until then anyone
can test it locally by setting the same key on the Config tab.

## What a share does

`POST /share` with `{ "file": "coast-road-sprint.json", "stage": {...} }`.
The worker checks that the name is a plain slug, that the body is a codriver
stage with a line and notes and under 3 MB, then on the stages repo: reads
the base branch, creates a branch `share/<name>-<id>`, writes
`stages/<file>` on it (an update if the file exists), and opens a pull
request titled *Add stage: <race>* with length, note count and the author
name from the file. The app shows the pull request link. When the maintainer
merges, the index is rebuilt by the repo's own action and the stage appears
in every install's Community list.

If the relay is unreachable or refuses, the app falls back to what it did
before: it opens the share folder and the upload page, and says why.

## Abuse

Anyone who knows the URL can open pull requests. That costs you a click on
*Close*. The worker refuses anything that is not a valid stage, caps the
size, and Cloudflare's own limits sit in front. If it ever becomes a problem,
the first step is a shared secret the app sends along, the second is rate
limiting with Workers KV. Neither is needed for a community of rally drivers.
