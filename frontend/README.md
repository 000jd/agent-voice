# agent-voice web UI

A blank, production-grade Svelte 5 + SvelteKit 3 scaffold for the local `agent-voice`
daemon. It is a **static SPA** built for `/ui`, with no server-side rendering and no
backend logic. The daemon, CLI, MCP server and Python package are unchanged.

This is a preview, not a feature release. The dashboard reads `/health` and
`/v1/engines`; the other routes are explicit placeholders.

## Requirements

- **Node 24 LTS** (`24.22.3` pinned in `.nvmrc`). Node 23 is not supported by the
  current Vite/Vitest/ESLint toolchain.
- **Bun 1.4.5** for installs and scripts. Bun never runs the toolchain itself; the
  binaries still use Node (`bun run`/`bunx`, never `--bun`).
- The daemon for live data: `uv run agent-voice serve` from the repository root.

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
to `http://127.0.0.1:8000`. It rewrites `Origin` to the daemon's own loopback origin
and injects `Authorization` when a dev key is configured, so the unchanged guard
accepts the request. A browser cannot use the proxy to widen the guard.

If the daemon has `AGENT_VOICE_API_KEY` set, create `frontend/.env` (gitignored):

```bash
AGENT_VOICE_DEV_API_KEY=the-same-value
```

Restart Vite after changing it. Never use a `VITE_*` or `PUBLIC_*` spelling: those
are exposed to the browser bundle. The value stays in the Vite process.

## Scripts

| Command                 | What it does                                          |
| ----------------------- | ----------------------------------------------------- |
| `bun run dev`           | Vite dev server at `/ui/`, with the guard-safe proxy  |
| `bun run build`         | static build in `build/`, entry `200.html`            |
| `bun run check`         | `svelte-kit sync` + `svelte-check --fail-on-warnings` |
| `bun run check:svelte5` | rejects Svelte 4 syntax and Kit 2 aliases             |
| `bun run lint`          | Prettier check + ESLint                               |
| `bun run format`        | Prettier write                                        |
| `bun run test`          | Vitest component tests (happy-dom)                    |
| `bun run test:e2e`      | Playwright smoke tests (Chromium)                     |
| `bun run gen:api`       | export OpenAPI, regenerate `schema.d.ts`              |
| `bun run check:api`     | fail if either generated file is stale                |
| `bun run preview`       | Vite preview of the production build                  |

## API types

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
with this generated schema. The path-based form stays fully typed. `/health` and
`/v1/engines` currently return bare dicts without response models, so
`src/lib/api/dashboard.ts` casts the raw envelope and restores a checked shape with
local guards. Adding `response_model` to those backend routes is the follow-up that
removes the cast; it is a backend change, not part of this scaffold.

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

The daemon currently does not mount `/ui`; that is the next backend release. See
`docs/frontend/architecture.md` for the exact contract and packaging plan.

## Security and offline behaviour

- Same-origin only. The UI is another REST client; it has no auth system and never
  edits Tier-A settings (`api_key`, bind, allowed hosts, path confinement, engine
  mutation, toolset, homes/dirs).
- No external runtime requests: Inter is self-hosted via Fontsource, icons are
  bundled from `@lucide/svelte`, and the only network traffic is to the daemon.
- No telemetry, analytics, service worker or CDN.
- `/audio/<id>` is bearer-protected. A future player must `fetch` it with the key and
  use `URL.createObjectURL`; a bare `<audio src="/audio/x">` cannot send `Authorization`.
- WebSocket auth uses `Sec-WebSocket-Protocol: bearer, <key>`; SSE needs `fetch` +
  `eventsource-parser` because `EventSource` cannot set headers. Neither is wired yet.

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
