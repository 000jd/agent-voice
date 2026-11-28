# agent-voice web UI

A local studio for the `agent-voice` daemon. It is a **static SPA** served at `/ui`,
with no server-side rendering and no backend logic. The v0.7 daemon ships the built
assets under `src/agent_voice/web/`; Node and Bun remain development-only.

The v0.7 Studio is the final **Graphite & Brass** design: one navigation rail, a
session-key gate, durable history, SSE progress and the browser draft/trim clone
flow. There is no server-path field anywhere in the UI. See
`docs/frontend/architecture.md` and the design-system document before changing UI.

## Requirements

- **Node 24 LTS** (`24.22.3` pinned in `.nvmrc`). Node 23 is not supported by the
  current Vite/Vitest/ESLint toolchain.
- **Bun 1.4.5** for installs and scripts. Bun never runs the toolchain itself; the
  binaries still use Node (`bun run`/`bunx`, never `--bun`).
- The daemon for live data: `uv run agent-voice serve` from the repository root.
- For live browser tests only: `uv` and `ffmpeg` on the machine. Playwright starts an
  isolated mock daemon itself, so no GPU is needed.

## Run it

```bash
# terminal 1, repository root
uv run agent-voice serve

# terminal 2
cd frontend
nvm use            # reads .nvmrc
bun install
bun run dev        # http://127.0.0.1:5173/ui/
```

The Vite dev server binds `127.0.0.1` and proxies only `/health`, `/v1` and `/audio`
to `AGENT_VOICE_DEV_URL` (default `http://127.0.0.1:8000`). It rewrites `Origin` to
the daemon's own loopback origin and injects `Authorization` when a dev key is
configured, so the unchanged guard accepts the request. A browser cannot use the
proxy to widen the guard.

If the daemon has `AGENT_VOICE_API_KEY` set, create `frontend/.env` (gitignored):

```bash
AGENT_VOICE_DEV_API_KEY=the-same-value
```

Restart Vite after changing it. Never use a `VITE_*` or `PUBLIC_*` spelling: those
are exposed to the browser bundle. The value stays in the Vite process.

## Screens

| Screen     | What it does                                                                               |
| ---------- | ------------------------------------------------------------------------------------------ |
| Speak      | Three-pane voice → script → inspector workspace, SSE jobs, durable takes, docked player    |
| Voices     | Voice grid and one-card-per-speaker presets; clone (upload/record/trim/consent) and design |
| Transcribe | Upload or record, persistent captures, editable transcript, server SRT/VTT/TXT/JSON        |
| Library    | Unified durable history and captures, filters, favourites, replay, re-render               |
| Models     | Engine cards, install/device/weights/loaded/VRAM state, unload all                         |
| Settings   | Safe settings, read-only security keys, diagnostics, MCP config, lexicon, cache            |

The visual system and anti-slop rules are in
[`docs/frontend/design-system.md`](../docs/frontend/design-system.md). Read it with
`AGENTS.md` before changing a token or screen.

## Scripts

| Command                 | What it does                                                |
| ----------------------- | ----------------------------------------------------------- |
| `bun run dev`           | Vite dev server at `/ui/`, with the guard-safe proxy        |
| `bun run build`         | static build in `build/`, entry `200.html`                  |
| `bun run check`         | Kit sync + `svelte-check --fail-on-warnings` + scoped `tsc` |
| `bun run check:types`   | scoped TypeScript-only gate                                 |
| `bun run check:svelte5` | rejects Svelte 4 syntax and Kit 2 aliases                   |
| `bun run check:spa`     | serves `build/` and asserts the `/ui/` fallback contract    |
| `bun run lint`          | Prettier check + ESLint                                     |
| `bun run format`        | Prettier write                                              |
| `bun run test`          | Vitest unit/component tests (happy-dom)                     |
| `bun run test:e2e`      | daemon-served Playwright tests, Axe and screenshots         |
| `bun run build:web`     | rebuild `src/agent_voice/web` + CSP hashes                  |
| `bun run demo:seed`     | seed a running mock daemon for visual review                |
| `bun run gen:api`       | export OpenAPI, regenerate `schema.d.ts`                    |
| `bun run check:api`     | fail if either generated file is stale                      |
| `bun run preview`       | Vite preview of the production build                        |

`bun run check` includes a scoped `tsc` because `tsc` cannot model named exports from
generated shadcn `.svelte` modules. `svelte-check` owns those components. Do not remove
the TS-only gate to make a component compile.

## Live browser tests and screenshots

`bun run test:e2e` builds the SPA into the package and starts the real daemon at
`http://127.0.0.1:8765/ui/` with `mock` + `mock_asr`, a temporary data dir and the
real manifests. It covers navigation, theme, command palette, live Speak playback,
upload → trim → clone → save, transcription/export, the library, all screens in both
themes with Axe, and the screenshot matrix under `docs/frontend/screenshots/`.

`bun run demo:seed` adds four voices, preset renders and a capture to a running mock
daemon. Both the suite and the seed use the public REST API; no user cache or GPU
state is touched.

## API types and guards

`openapi.json` is produced by `scripts/export_openapi.py` from the real FastAPI app,
using a temporary home/data/engines directory and `mount_mcp=False`. The exporter is
deterministic (`sort_keys`, two-space indent, trailing newline) and never starts a
worker or imports torch. `openapi-typescript@7.13.0` turns it into
`src/lib/api/schema.d.ts`; both files are committed.

`src/lib/api/client.ts` uses openapi-fetch's **path-based** client:

```ts
import { api } from '#lib/api/client.js';

const result = await api['/health'].GET();
```

The method-based `api.GET('/health')` form collapses to `never` under TypeScript 6
with this generated schema. Most read routes return bare dicts without response
models, so `src/lib/api/guards.ts` casts the raw envelope and restores a checked
shape. Components consume the domain types, not `unknown`.

`/v1/agent/speak` is the studio synthesis path. `/v1/audio/transcriptions` is
multipart and returns the canonical `verbose_json` result when asked. Audio is
fetched with `fetch` + object URL, never a bare `<audio src>`.

## Build and serving contract

`bun run build` writes:

- `build/200.html` — the SPA fallback (not `index.html`; a pure SPA has no index)
- `build/_app/...` — hashed assets, referenced as `/ui/_app/...`
- `build/_app/version.json`

The host serves `/ui/*` from `build/`, but:

- `/ui/_app/*` must **404** when the file is missing, never fall back to HTML;
- every other `/ui/*` path (including `/ui/voices` on a hard refresh) returns
  `build/200.html`;
- `/ui` redirects to `/ui/`.

`bun run check:spa` proves this locally against the built output; the daemon mounts
`/ui` when `AGENT_VOICE_UI=on` (default) and serves the same contract with a strict
CSP that includes hashes for SvelteKit's inline bootstrap scripts.

## Security and offline behaviour

- Same-origin only. The UI is another REST client. A keyed daemon is unlocked with a
  session key validated by `/v1/auth/check`; it lives in memory/sessionStorage, never
  localStorage or a URL. The UI never edits Tier-A settings (`api_key`, bind, allowed
  hosts, path confinement, engine mutation, toolset, homes/dirs).
- No external runtime requests: Inter is self-hosted via Fontsource, icons are
  bundled from `@lucide/svelte`, and the only network traffic is to the daemon.
- No telemetry, analytics, service worker or CDN.
- `/audio/<id>` is bearer-protected. The player fetches it with same-origin
  credentials and uses `URL.createObjectURL`; on a keyed daemon it shows the 401
  explanation instead of a fake player. It never stores the key.
- WebSocket auth uses `Sec-WebSocket-Protocol: bearer, <key>`; SSE uses authenticated
  `fetch` streaming with `Last-Event-ID`, because `EventSource` cannot set headers.
  The token never appears in a URL.
- The clone dialog uses draft upload only. It never asks for a server path; the
  session manifest, limits and paths come from `/v1/server_info`.

## Svelte 5 rules

This project is runes-only and Kit 3-only. Read `AGENTS.md` before changing code.
The short version: use `$state`/`$derived`/`$props`, snippets, `onclick` attributes
and callback props; never `export let`, `$:`, `on:click`, `<slot>`, slots,
`createEventDispatcher`, `<svelte:component>`, `$app/stores`, `$lib`, or a
`svelte.config.js`. Import through `#lib`.

## Dependencies and licences

Every direct dependency is pinned exactly in `package.json`; `bun.lock` is committed.
`bunfig.toml` refuses packages newer than 14 days and install scripts. The only
peer waiver is `openapi-typescript`'s TypeScript `^5` declaration under the
required TypeScript 6. Run `bun audit --audit-level=high`. The full version,
publication-age and licence table is in `docs/frontend/phase0-research.md`.
