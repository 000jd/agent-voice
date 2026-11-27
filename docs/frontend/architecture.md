# Frontend architecture and `/ui` serving contract

Status: **scaffold only**. `frontend/` builds a static SPA, but the daemon does not
mount it yet. No Python route, guard rule, installer or engine was changed.

## Boundaries

- The UI is one more REST client of the local daemon. There is no UI-only endpoint,
  no UI-side auth, and no UI flag that changes Python behaviour.
- The daemon stays headless-capable. CLI and MCP users never need Node, Bun or a
  browser. Python installation never runs a JavaScript toolchain.
- Same origin. The UI calls `/health`, `/v1/...` and `/audio/...`; the daemon serves
  the static files at `/ui` in a later release.
- Security-sensitive settings stay read-only in the UI: bind/auth, allowed hosts,
  clone-path confinement, engine mutation/approval, toolset, and the home/data/engines
  paths.

## Build shape

`frontend/vite.config.ts` uses `sveltekit()` with:

```ts
adapter({
  pages: 'build',
  assets: 'build',
  fallback: '200.html',
  precompress: false,
  strict: true
});
paths: { base: '/ui', relative: false }
```

plus a root `+layout.ts` with `ssr = false`, `prerender = false`,
`trailingSlash = 'never'`.

The output is:

```text
frontend/build/
  200.html
  _app/version.json
  _app/immutable/...
```

There is no `build/index.html` and no `/ui` prefix inside the output tree. Asset URLs
inside `200.html` are absolute `/ui/_app/...`.

## Serving contract (for the future daemon mount)

The host must implement this exactly:

1. `GET /ui` → redirect to `/ui/`.
2. `GET /ui/_app/<path>` → serve the file, or **404** if missing. Never fall back
   to HTML for a missing hashed asset.
3. `GET /ui/<path>` → serve the file if it exists; otherwise serve `200.html` with
   HTML content type.
4. Do not serve the SPA for `/v1`, `/audio`, `/mcp`, `/health`, `/docs` or
   `/openapi`.

A hard refresh on `/ui/voices` must return `200.html`; a missing
`/ui/_app/immutable/...js` must stay a 404.

Do not rely on Starlette's default 404 path for the fallback: `api/app.py` has a
`StarletteHTTPException` handler that rewrites every 404 into the project's JSON
error envelope. Use an explicit `/ui` route (or a dedicated static subclass) so a
SPA route and a missing asset remain distinguishable.

`/ui` is deliberately **not** under the guard's bearer prefixes (`/v1`, `/audio`,
`/mcp`), because a browser fetching static assets cannot attach `Authorization`.
Host and Origin checks still apply. The API calls the page makes remain protected
when `AGENT_VOICE_API_KEY` is set; a later token-entry flow must use a bearer header,
not a query string.

## Development proxy

Vite proxies only `/health`, `/v1` and `/audio` to `http://127.0.0.1:8000`:

- `changeOrigin: true` rewrites `Host` to the daemon;
- `headers.Origin` is rewritten to the daemon's loopback origin, so the unchanged
  Host/Origin guard accepts it;
- `Authorization: Bearer $AGENT_VOICE_DEV_API_KEY` is injected when that variable is
  present in the Vite process (from `frontend/.env` or the environment);
- `ws: true` is enabled for `/v1`; the UI does not use a socket yet.

The dev key must never be spelled `VITE_*` or `PUBLIC_*`; those are browser-visible.
A browser cannot use the proxy to bypass the guard; only requests that reach the
proxy get the rewrite, and a foreign direct request is still 403.

## API typing

- `scripts/export_openapi.py` builds the FastAPI app with a temporary
  home/data/engines, an empty registry, `mount_mcp=False`, and explicit settings. It
  writes sorted JSON and never starts a worker or imports torch.
- `frontend/openapi.json` and `frontend/src/lib/api/schema.d.ts` are committed
  generated artifacts. `bun run check:api` fails if either is stale.
- `src/lib/api/client.ts` uses openapi-fetch's path-based client:
  `api['/health'].GET()`. The method-based `api.GET('/health')` form infers `never`
  under TypeScript 6 with this schema.
- `/health` and `/v1/engines` return bare dicts without `response_model`, so their
  generated error branch is `never`. `src/lib/api/dashboard.ts` casts only the raw
  envelope and validates the payload with local guards. The backend follow-up is to
  add response models; the UI should then drop the cast and guards.
- `openapi-typescript@7.13.0` declares TypeScript `^5`; the project uses TS 6 for Kit
  3. This is a generation-only peer waiver, recorded in the Phase 0 report.

## Audio, SSE and WebSocket (documented, not implemented)

- `/audio/<id>` requires a bearer when `AGENT_VOICE_API_KEY` is set. A future player
  must `fetch` the artifact with the header and play `URL.createObjectURL(blob)`.
  `<audio src="/audio/...">` cannot send the header.
- SSE: use `fetch` + `eventsource-parser`; `EventSource` cannot set headers.
- WebSocket: `Sec-WebSocket-Protocol: bearer, <key>` is the accepted browser-safe
  spelling; the server echoes the matched protocol. The route `/v1/audio/ws` is not
  in the OpenAPI document.

## Distribution at release time

This scaffold keeps `frontend/build/` ignored. A tagged source clone still installs
with `uv sync` and needs no Node.

The UI-readiness release will:

1. build `frontend/` in CI on a pinned Node/Bun;
2. copy `frontend/build/` into package data under `src/agent_voice/web/` (or an
   equivalent shipped directory) in the release commit;
3. mount that directory with the serving contract above;
4. verify `uv build` contains the built assets and still excludes source
   `frontend/`.

That is a backend change and must be reviewed as one. Until then, the only supported
way to see the UI is `bun run dev` from `frontend/`, or serving `frontend/build/`
with a host that implements the contract.

## Tests and gates

- `bun run check` — Kit sync + `svelte-check --fail-on-warnings`.
- `bun run check:svelte5` — line-level Svelte 4 / Kit 2 blocklist.
- `bun run lint` — Prettier + ESLint 10.
- `bun run test` — Vitest 5, happy-dom, Testing Library; app shell and dashboard
  loading/error/success.
- `bun run test:e2e` — Playwright 1.65, Chromium; every stub route, theme toggle,
  dashboard error state.
- `bun run build` — static output; then a fallback server must prove `/ui/`,
  `/ui/voices`, `_app` 404 and an asset 200.
- `bun run check:api` — exporter + OpenAPI types are current.
- `bun audit --audit-level=high` — blocking supply-chain gate.
- Python: `uv run ruff check .`, `uv run pytest -m "not gpu"`, `uv build` file list.

## Deferred screens

Nav stubs only: Speak, Voices, Transcribe, Models, History, Settings. Later screens
(not in the nav yet): `/script`, `/lexicon`, `/dub`, `/agents`, `/diagnostics`.
Player/chart work uses `wavesurfer.js` / `uplot` only after the UI-readiness release;
`wavesurfer-multitrack` is incompatible with wavesurfer 8.
