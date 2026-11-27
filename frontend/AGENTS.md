# Agent rules for `frontend/`

The UI is a static REST client of the local daemon. It must not grow backend logic,
must not change the guard, and must not make Node/Bun a runtime requirement for
Python users.

## Stack and hard rules

- **Svelte 5 runes only.** `$state`, `$derived`/`$derived.by`, `$effect`, `$props`,
  `$bindable`; snippets and `{@render}`; event attributes (`onclick`); callback props.
- **No `svelte.config.js`.** Kit 3 config lives in `sveltekit()` in `vite.config.ts`.
- **No `$lib`.** Use `#lib` subpath imports and keep module extensions
  (`.js` for TS source, `.svelte` for components).
- **No Kit 2 APIs:** `$app/stores` → `$app/state`; `$app/environment` → `$app/env`;
  `$app/paths.base/assets` → `resolve()`/`asset()`; `invalidateAll` → `refreshAll`;
  `json()`/`text()` → `Response.json()`/`Response.text()`.
- **No Svelte 4 syntax:** `export let`, `$$props`, `$$restProps`, `$:` statements,
  `on:click`/event modifiers, `<slot>`, slot attributes, `let:`, `createEventDispatcher`,
  `new Component({ target })`, `<svelte:component>`, `<svelte:self>`,
  `beforeUpdate`/`afterUpdate`, or a store `writable()` for local state.
- Do not name a component-local variable `state` when the file also uses `$state`;
  prefer `view`, `model`, `data`. This exact name collision breaks `svelte-check`'s
  rune detection.
- Prefer `$derived(expr)` or `$derived.by(() => ...)`; never `$derived(fn)`.
- `frontend/scripts/check-svelte5.mjs` enforces the blocklist. Run it.

## Repository boundaries

- Do not edit `src/agent_voice/`, `engines/`, or `tests/` from a frontend task.
  The only approved Python-side files are `scripts/export_openapi.py` and its test.
- The only daemon contract used here is REST. No UI-only endpoint, no UI flag that
  changes Python behaviour, no auth/login.
- Security-sensitive settings are read-only from the UI: `api_key`, `host`,
  `allowed_hosts`, `allow_any_path`, `mcp_base_path`,
  `allow_agent_engine_mutation`, `require_engine_approval`, `toolset`, and the
  home/data/engines paths.
- Same origin only. Do not add CORS, a public dev tunnel, or an external request.

## API client

- Generated files are committed and must not be hand-edited:
  `openapi.json` and `src/lib/api/schema.d.ts`. Change the daemon (elsewhere) and run
  `bun run gen:api`.
- Use the path-based client: `api['/health'].GET()`. The `api.GET('/health')` form
  infers `never` under TypeScript 6.
- `/health` and `/v1/engines` lack response models; keep their casts and parsing
  guards in `src/lib/api/dashboard.ts`. Do not spread `any` through components.
- Audio and WebSocket details are documented, not implemented: `/audio/<id>` needs a
  bearer `fetch` + object URL; WS uses `Sec-WebSocket-Protocol: bearer, <key>`; SSE
  needs `fetch` + `eventsource-parser`.

## Dependencies

- Pin exact versions (`bun add --exact`), never ranges; commit `bun.lock`.
- New direct dependencies need an exact version, a licence, a publication date and an
  audit entry in `docs/frontend/phase0-research.md`. Prefer releases at least 14 days
  old; Bun's `minimumReleaseAge` enforces it.
- Do not add a telemetry, analytics, external-font or CDN dependency. Do not add
  `sonner`, charts, the shadcn `sidebar` bundle, or another UI library for a preview.
- Do not add install scripts. `bunfig.toml` sets `ignoreScripts = true`; native
  packages in this stack use optional platform packages, not lifecycle builds.
- Run `bun audit --audit-level=high`; there is no "fix later" for high/critical.

## Verification before claiming done

From `frontend/`:

```bash
bun run check
bun run check:svelte5
bun run lint
bun run test
bun run build
bunx playwright test          # Chromium must be installed
bun run check:api
bun audit --audit-level=high
```

`bun run check` is `svelte-check`, not `tsc --noEmit`: tsc cannot model named exports
from `.svelte` files and will fail on generated shadcn primitives. `svelte-check` is
the type gate.

Anything not actually run must be labelled `UNVERIFIED` with the exact reason.
