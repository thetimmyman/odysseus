# Dev Preview — fork/PR productization plan (shape for review)

**Status: planning.** The feature works end-to-end on the Framework today (loopback dev server +
admin-gated proxy + iframe preview, DB-backed routes live). This doc is the *shape* of the work to turn
it from "Tim's Framework setup" into a clean, configurable, documented feature suitable for a fork/PR —
to be reviewed **before** any settings/secrets UI is built. Order (agreed): docs/plan → read-only
Settings + Security Status → app config → masked env editor (separate focused pass).

## 1. What the feature already is (PR scope / file inventory)
| File | Role |
|---|---|
| `src/dev_preview.py` | process manager: scan apps, install (npm), start/stop (loopback bind), status+reconcile, kill-by-port, capped logs, secret-scrubbed child env |
| `src/dev_preview_proxy.py` | 2nd uvicorn on `:PROXY_PORT`; admin-cookie gate, CSRF (Origin/Fetch-Metadata) on unsafe methods + WS, HTTP+WS proxy to `127.0.0.1:DEV_PORT`, strips only frame-blockers |
| `routes/dev_preview_routes.py` | `/api/dev-preview/{apps,install,start,stop,status,logs}`, all `require_admin_cookie` |
| `core/middleware.py` | CSP `frame-src` adds `http://<request-host>:PROXY_PORT` so the SPA can iframe the proxy |
| `app.py` | router include + proxy server launched in `@on_event("startup")` |
| `docker-compose.yml` | `:3000` NOT published; `${APP_BIND}:PROXY_PORT` published; searxng digest-pinned |
| `Dockerfile` | `wget` (apps shell out for outbound HTTP), `git-lfs` + `git config --system safe.directory '*'` |
| `static/js/devPreview.js`, `static/index.html`, `static/style.css`, `static/sw.js` | Tools entry → overlay; apps rail, controls, logs, iframe |

## 2. Already PR-shaped vs. needs work
**Already good:** backend config is env-driven (`DEV_PREVIEW_ROOT`=`/app/work`, `DEV_PREVIEW_PORT`=3000,
`DEV_PREVIEW_PROXY_PORT`=7100); no hardcoded paths/users in *code*; `require_admin_cookie` non-optional;
loopback-bind + frame-strip are fixed in code; searxng pinned; the personal SSH-tunnel/`SSH_USER`
assumption was removed.
**Needs work:** surface config in a UI (not just env); per-app config + env status; a read-only security
status panel; de-hardcode the compose bind mount; docs.

## 3. Config model (the core design decision)
Introduce a single admin-owned `dev_preview` config blob in Odysseus's existing settings store
(`data/settings.json`), read with precedence **settings → env → default**. Keys: `repos_root`, `enabled`,
`proxy_port`, `dev_port`, `app_allowlist` (optional; default = all package.json dirs), `package_manager`
(npm; pnpm/yarn later). The 3 env vars become the *fallback*, the UI the *source of truth*.

**Not everything is freely runtime-configurable — be explicit in the UI about coupling:**
- `proxy_port` is **deployment-coupled**: it must equal a host-published Compose port (`${DEV_PREVIEW_PROXY_PORT}:...`)
  AND the proxy server must restart to rebind. So the UI **displays** it and any change is flagged
  *deployment-level* (requires editing Compose + a restart) — it cannot take effect from a UI write alone.
  (The `core/middleware.py` frame-src also derives from it; another reason it's not a free toggle.)
- `dev_port` is the **container-internal loopback** port (not published) — runtime-safe to change (the
  proxy forwards to it); just must not collide with `proxy_port`.
- `repos_root` is the **container** path the scanner walks (default `/app/work`). The **host** directory
  bind-mounted onto it is a *separate, deployment-level* value (`REPOS_HOST_DIR` in Compose, §7). The UI
  must present these as two distinct things — editing `repos_root` only re-points the scan *inside the
  container*; changing what host folder is mounted is a Compose/restart operation, not a UI write.
- `enabled`, `app_allowlist`, `package_manager` — genuinely runtime-configurable.

## 4. The four UI surfaces
| Surface | Purpose | Backend | Fixed vs configurable |
|---|---|---|---|
| **Dev Workspace Settings** | repos root, enable toggle, proxy/dev ports, app allowlist, PM preference, git identity + SSH-key status | `GET/PUT /api/dev-preview/config` (admin) | configurable: paths/ports/allowlist/PM |
| **App-Level Config** | per selected app: detected install cmd (not arbitrary shell), start-script picker (have `dev_scripts`), env status `missing/partial/ready` | extend `/apps` + a `/app/{id}` detail | configurable: which script, which app |
| **Security Status** | read-only: dev-server-unpublished ✓, proxy-admin-gated ✓, siblings-cannot-reach ✓, service-role present/missing; loud warning on any bind/exposure edit | new `GET /api/dev-preview/security-status` (turn the existing test-probes into a live check) | **read-only** |
| **Masked env editor** | view keys as `••••• (set)` / `(blank)`, set/clear values for `.env.local` | `GET` (keys+status only) / `PUT` (write-only) — **own section §6** | configurable: values; never returns plaintext |

## 5. Fixed security invariants (NOT UI-toggleable)
These stay in code; only an operator editing deployment config can change them. The Settings UI must
**display** them (Security Status) but not offer switches:
- proxy **requires admin cookie** (+ rejects bearer / internal-tool / cross-site)
- dev server **binds container loopback only**; `:DEV_PORT` is **never host-published**
- proxy strips **only** `X-Frame-Options` + CSP `frame-ancestors` (rest of app CSP intact)
- proxy upstream is **fixed** to the active dev server (never an arbitrary URL)

## 6. Masked env editor (the sharp edge — build LAST, own review)
Rules, non-negotiable:
- **Admin-cookie only**, owner-scoped; same gate as the rest.
- **Write-only values.** `GET` returns key names + `set|blank` only — never plaintext. `PUT` accepts
  `{key, value}` and writes to the app's `.env.local`.
- **Never print / never log** values (server-side or client). No values in responses, toasts, or logs.
- **Assert `.gitignore`** covers `.env.local` before any write; refuse if not ignored. Write `0600`.
- **Sourcing from a vault** (k3s/Vaultwarden) is a separate opt-in flow, also value-never-printed.
- Show a "production credentials" warning; recommend a dev/staging Supabase over prod where possible.

## 7. De-hardcode checklist (cleanup before PR)
- Compose bind mount → `${REPOS_HOST_DIR:-./repos}:/app/work` — **generic default is `./repos`** (repo-relative,
  PR-clean). A deployment overrides it in `.env`; this Framework one sets `REPOS_HOST_DIR=/mnt/framework-data/repos`.
  The *container* path `/app/work` stays the `DEV_PREVIEW_ROOT` default (distinct from the host dir — see §3).
- Confirm no Tacticus-specific copy in generic UI (today: none — "tacticus" only appears as a *detected
  app*). Keep it that way.
- Ports already `${APP_BIND}`/env-driven — keep consistent; add `${DEV_PREVIEW_PROXY_PORT}` to compose
  instead of the literal `7100`.
- searxng digest pin (done) — note in PR so it isn't "reverted to latest."

## 8. Image dependencies (document in the PR)
The Dev Preview image must carry common app build/runtime utilities; previewed Next apps assume them:
`nodejs`, `npm`, `git`, **`git-lfs`** (LFS repos + pre-push hook), **`wget`** (apps shell out for
outbound HTTP to dodge Next's patched fetch — this is what unblocked the DB health check), `ripgrep`.
Document this list + *why* so a fork maintainer doesn't strip them.

## 9. Docs to write (in the PR)
`docs/dev-preview.md`: setup; **threat model** (why the proxy exists, why `:DEV_PORT` is never exposed,
the sibling-container reasoning); how `.env.local`/secrets are handled (write-only, never committed);
how the GitHub SSH key is configured (deploy key, `--no-verify` hook note).

## 10. Build sequencing
1. **This doc** (review the shape). ← you are here
2. De-hardcode cleanup (§7) + the config model (§3, read path).
3. **Read-only Settings + Security Status** panels (prove the config plumbing; zero secret risk).
4. App-level config (install/start/env-status; no secrets yet).
5. **Masked env editor** — separate, focused, security-reviewed pass (§6).
6. Docs (§9) alongside, finalized at PR time.

## 11. Open questions for you
- **Prod vs staging Supabase for preview.** Today `.env.local` points at *prod* (service-role = RLS
  bypass). Loopback+proxy contains exposure, but do we want a dev/staging target as the documented
  default, with prod as an explicit opt-in?
- **Where does the PR land** — upstream `odysseus` (generic, no Tacticus assumptions) or your fork only?
- **One PR or split** (process-manager+proxy; then UI; then masked editor)? Recommend split — the proxy
  is reviewable on its own; the masked editor wants its own scrutiny.
