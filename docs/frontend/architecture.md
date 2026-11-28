# Frontend architecture and `/ui` serving contract

Status: **v0.7 Studio shipped**. `frontend/` builds the static SPA and the daemon
serves the packaged copy at `/ui`; `AGENT_VOICE_UI` and `serve --no-ui` disable it.
The final UI is the original Graphite & Brass system: one rail, six screens, a
session-key gate, durable history/captures, authenticated SSE and browser-only draft
cloning. The design tokens live in `docs/frontend/design-system.md`.

## Boundaries

- The UI is one more REST client of the local daemon. There is no UI-only endpoint
  and no UI flag that changes Python behaviour. A keyed daemon is unlocked by a
  session key validated through `/v1/auth/check` and held in memory/sessionStorage.
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

## Serving contract (implemented by `routes_ui.py`)

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
`/mcp`), because static assets must load before the key is entered. Host and Origin
checks still apply. The API calls the page makes remain protected when
`AGENT_VOICE_API_KEY` is set; the UI validates the key with `/v1/auth/check`, stores
it in memory/sessionStorage (never localStorage or a URL), and clears it on any 401.

The CSP is strict (`default-src 'self'`, no object/frame embedding) and includes
SHA-256 hashes for SvelteKit's inline bootstrap scripts. `scripts/build_web.py`
computes those hashes into `csp-sha256.json`; `routes_ui.py` appends them to
`script-src` when the app starts. Rebuild the web assets after changing the app
shell, or the browser will reject the inline bootstrap.

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

- **Shell:** one 224/64px rail (Speak, Voices, Transcribe, Library, Models, Settings),
  a 56px top bar with title, command palette (Ctrl/Cmd+K), connection + GPU chip,
  theme and help, and one docked player. Mobile uses a two-row bottom rail.
- **Speak:** three independent panes — searchable voice/preset picker, markup script
  editor with takes, and a voice-derived inspector. Jobs stream over authenticated
  SSE; takes are durable history and can be compared, favourited, downloaded,
  re-rolled or deleted.
- **Voices:** a voice grid plus one card per preset speaker. Clone is a three-step
  browser flow (upload/record → trim + quality + transcript → name + consent) that
  sends only a draft id; design auditions takes. There is no server-path field.
- **Models:** real engine/model state, device variants, install/reinstall, load/unload
  and unload-all. A multi-GB download is confirmed before it starts.
- **Transcribe:** upload or record, server-side ASR, editable persisted transcript,
  timed segments and server-generated SRT/VTT/TXT/JSON; a capture can become a draft.
- **Library:** unified durable history and captures with search, filters, favourites,
  replay, re-render and delete.
- **Settings:** grouped safe settings with source/shadow display, read-only security
  keys, diagnostics, MCP config, lexicon and cache controls; restart-required is
  stated. Auth is a session-key gate, not a stored credential UI.

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
- `bun run test` — Vitest 5, happy-dom; format, markup, player, SSE parsing, voice
  compatibility, preset deduplication and Speak request construction.
- `bun run test:e2e` — Playwright 1.65 builds the SPA and starts an isolated daemon
  serving `/ui` with mock TTS/ASR. It covers navigation/theme/palette, live Speak,
  upload → trim → clone → save, transcription/export, the library, both themes with
  Axe (0 serious/critical), and the committed screenshot matrix in
  `docs/frontend/screenshots/`. It needs `uv` and `ffmpeg`.
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

## Honest limits

Microphone recording works through `MediaRecorder` in Chromium and Firefox; WebKitGTK
and Safari recording are not verified. The Studio uses authenticated fetch streaming,
not a WebSocket. No UI-only backend route exists; add a backend contract first rather
than inventing one in the client.
