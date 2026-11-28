# Frontend architecture and `/ui` serving contract

Status: **v0.7 backend wired, studio visual rewrite pending**. `frontend/` builds the
static SPA and the daemon now serves the packaged copy at `/ui`; `AGENT_VOICE_UI` and
`serve --no-ui` disable it. The v0.7 backend adds drafts, persistent history/captures,
SSE, settings and previews. The original Graphite & Brass visual system and the new
information architecture are Part 2 work; the current preview shell is not the final
design.

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
- The UI never invents a REST surface. Where the API has no field (languages, usage,
  voice previews, an exact inbox path), the screen says so instead of fabricating it.

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
`/ui/_app/immutable/...js` must stay a 404. `bun run check:spa` is the local proof.

Do not rely on Starlette's default 404 path for the fallback: `api/app.py` has a
`StarletteHTTPException` handler that rewrites every 404 into the project's JSON
error envelope. Use an explicit `/ui` route (or a dedicated static subclass) so a
SPA route and a missing asset remain distinguishable.

`/ui` is deliberately **not** under the guard's bearer prefixes (`/v1`, `/audio`,
`/mcp`), because a browser fetching static assets cannot attach `Authorization`.
Host and Origin checks still apply. The API calls the page makes remain protected
when `AGENT_VOICE_API_KEY` is set; the UI never stores that key and never adds a
login form. Audio on a keyed deployment reports the 401 instead of pretending it can
play.

## Development proxy

Vite proxies only `/health`, `/v1` and `/audio` to `AGENT_VOICE_DEV_URL` (default
`http://127.0.0.1:8000`):

- `changeOrigin: true` rewrites `Host` to the daemon;
- `headers.Origin` is rewritten to the daemon's loopback origin, so the unchanged
  Host/Origin guard accepts it;
- `Authorization: Bearer $AGENT_VOICE_DEV_API_KEY` is injected when that variable is
  present in the Vite process (from `frontend/.env` or the environment);
- `ws: true` is enabled for `/v1`; the UI does not use a socket yet.

`AGENT_VOICE_DEV_URL` exists so the Playwright suite can point Vite at an isolated
mock daemon on port 8765 without touching a developer's port 8000.

The dev key must never be spelled `VITE_*` or `PUBLIC_*`; those are browser-visible.
A browser cannot use the proxy to bypass the guard; only requests that reach the
proxy get the rewrite, and a foreign direct request is still 403.

## API typing and guards

- `scripts/export_openapi.py` builds the FastAPI app with a temporary
  home/data/engines, an empty registry, `mount_mcp=False`, and explicit settings. It
  writes sorted JSON and never starts a worker or imports torch.
- `frontend/openapi.json` and `frontend/src/lib/api/schema.d.ts` are committed
  generated artifacts. `bun run check:api` fails if either is stale.
- `src/lib/api/client.ts` uses openapi-fetch's path-based client:
  `api['/health'].GET()`. The method-based `api.GET('/health')` form infers `never`
  under TypeScript 6 with this schema.
- Most read routes return bare dicts without `response_model`. `src/lib/api/guards.ts`
  casts only the raw envelope and validates the payload into domain types. Components
  consume those types and never touch `unknown`.
- `src/lib/api/studio.ts` owns endpoint calls, raw multipart upload and the single
  `ApiError` normalizer. Audio blobs are fetched there; the player owns object URLs.
- `openapi-typescript@7.13.0` declares TypeScript `^5`; the project uses TS 6 for Kit
  3. This is a generation-only peer waiver, recorded in the Phase 0 report.

## Studio screens

- **Shell:** full-width 52px studio bar (brand, Studio/Explore/System, voice search,
  primary action), 236/64px grouped rail below it, live connection badge and GPU/VRAM
  pill with unload. The two-tier player is docked inside the content column so the rail
  remains visible; mobile uses a menu panel instead of the rail.
- **Speak:** large editorial markup composer, capability-aware Clone/Preset/Design
  inspector with underline Setup/Session tabs, session list. `Ctrl/Cmd+Enter` generates; successful renders enter the
  global session store and the player.
- **Voices:** one table surface. Cloned/designed voices plus preset groups (one row per
  engine/model, with a speaker selector). Design is two-stage: audition 2–3 takes, then re-render the
  chosen take with `take`/`seed` and `save_as_clone`. Clone accepts a confined inbox
  path; the exact path is not exposed by REST, so the drawer shows the convention and
  the `agent-voice doctor` hint.
- **Models & Engines:** live installed/device/downloaded/loaded/VRAM state, grouped
  TTS/ASR, unload; no install/download mutation UI.
- **Transcribe:** multipart upload to `/v1/audio/transcriptions` with
  `response_format=verbose_json`; transcript, timed segments, client-side SRT/VTT.
  Inline truncation is stated, including the daemon-side JSON path.
- **Dashboard/History/Settings:** live status and recent session renders; History is
  explicitly session-local; Settings is read-only except theme.

## Audio player

`src/lib/stores/player.svelte.ts` is a single global player:

- fetch `/audio/<id>` with `credentials: 'same-origin'`;
- `URL.createObjectURL(blob)` for playback;
- `AudioContext.decodeAudioData` for 96 SVG peaks;
- play/pause, scrub, speed 0.75–1.5, volume, download and close;
- `Space` toggles unless focus is in an input/textarea/select/contenteditable;
- a 401 is surfaced as the daemon-key explanation.

`<audio src="/audio/...">` cannot attach the bearer header, which is why the fetch
path is mandatory.

## Tests and gates

- `bun run check` — Kit sync + `svelte-check --fail-on-warnings` + scoped
  `tsc -p tsconfig.tsc.json --noEmit`.
- `bun run check:types` — the scoped TS gate alone. Generated shadcn modules that
  export types from `.svelte` are excluded from tsc and stay under svelte-check.
- `bun run check:svelte5` — line-level Svelte 4 / Kit 2 blocklist.
- `bun run lint` — Prettier + ESLint 10.
- `bun run test` — Vitest 5, happy-dom, Testing Library; design tokens, format,
  markup, player state, Speak capability/composer/inspector, voices, transcription
  request, shell and dashboard.
- `bun run test:e2e` — Playwright 1.65 starts an isolated mock daemon and Vite, then
  runs navigation/theme/error, live Speak playback, design/save, transcribe upload/SRT,
  models, and desktop/mobile visual captures. It needs `uv` and `ffmpeg`.
- `bun run build` then `bun run check:spa` — static `/ui/` fallback and `_app` 404.
- `bun run check:api` — exporter + OpenAPI types are current.
- `bun audit --audit-level=high` — blocking supply-chain gate.
- Python: `uv run ruff check .`, `uv run pytest -m "not gpu"`, `uv build` file list.

## Distribution at release time

`frontend/build/` remains ignored. `scripts/build_web.py` builds it on pinned
Node/Bun and copies the result to `src/agent_voice/web/` with a content manifest;
`scripts/check_web_build.py` fails if the committed package data is stale. The daemon
mounts that directory with the serving contract above. `uv build` includes the assets
and excludes frontend source; a tagged clone + `uv sync` needs no Node.

## Deferred screens

The v0.7 backend now supplies drafts, durable history/captures, SSE, diagnostics,
lexicon/cache, settings and auth-check. Still deferred: a microphone capture flow,
the token-entry UI, a real WebSocket studio stream, and any UI-only backend route.
Do not add them by inventing an endpoint.
