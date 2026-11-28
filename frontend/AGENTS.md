# Agent rules for `frontend/`

The UI is a static REST client of the local daemon. It must not grow backend logic,
must not change the guard, and must not make Node/Bun a runtime requirement for
Python users.

The visual contract lives in [`docs/frontend/design-system.md`](../docs/frontend/design-system.md).
Read it before changing tokens, spacing or a screen.

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

## Studio design rules

- **Dark is default.** Light mode is a full token set, not an afterthought. Do not
  default from the OS colour scheme.
- Follow the Graphite & Brass tokens in `src/app.css`: `--canvas`, `--panel`,
  `--surface`, `--rule`, `--text`, `--signal`. Use tokens, not raw hexes.
- Brass `--signal` is the one accent: primary action, focus, selection and the played
  waveform. Success, danger and warning are state colours, not decoration.
- No gradients, glows, glass, emoji labels, fake KPIs, fake sparklines or invented
  metadata. Voice avatars are flat deterministic monograms, not gradient blobs.
- Every timestamp, duration, VRAM figure, seed and character counter is tabular
  (`font-variant-numeric: tabular-nums`; keep mono on metrics).
- The shell is one 224/64px rail (Speak, Voices, Transcribe, Library, Models,
  Settings) with a 56px top bar. No top tabs. The player is docked in the content
  column. Dark is default; light is a full token swap.
- The voice library is a grid of voice cards; presets are one card per speaker.
  Never render engine checkpoints as voices.
- The UI validates `/v1/auth/check` and gates the shell behind a session key
  (`sessionStorage` only). The key never goes in a URL or localStorage.
- Every screen has loading, empty, error and (where relevant) offline states. Do not
  leave a fake success state.

## Repository boundaries

- Do not edit `src/agent_voice/`, `engines/`, or `tests/` from a frontend task.
  The only approved Python-side files are `scripts/export_openapi.py` and its test.
- The only daemon contract used here is REST. No UI-only endpoint and no UI flag
  that changes Python behaviour. Auth is a key gate over the existing guard.
- Security-sensitive settings are read-only from the UI: `api_key`, `host`,
  `allowed_hosts`, `allow_any_path`, `mcp_base_path`,
  `allow_agent_engine_mutation`, `require_engine_approval`, `toolset`, and the
  home/data/engines paths.
- Same origin only. Do not add CORS, a public dev tunnel, or an external request.
- `GET /v1/server_info` provides the display paths, limits and UI state. Do not
  fabricate metadata the API does not return.

## API client and stores

- Generated files are committed and must not be hand-edited:
  `openapi.json` and `src/lib/api/schema.d.ts`. Change the daemon (elsewhere) and run
  `bun run gen:api`.
- Use the path-based client: `api['/health'].GET()`. The `api.GET('/health')` form
  infers `never` under TypeScript 6.
- Most responses have no OpenAPI response model. Parse them through
  `src/lib/api/guards.ts`; do not spread `unknown` or `any` into components.
- `/v1/agent/speak` responses are `SynthesisResult`; `/v1/voices/design` only returns
  a playable `download_url` for multi-candidate auditions. The two-step audition/save
  flow is intentional.
- Audio is fetch + object URL (`src/lib/stores/player.svelte.ts`), not a bare
  `<audio src>`; `/audio/<id>` may need a bearer. On 401 show the daemon-key message;
  never add a token store.
- Shared session/player state is in `src/lib/stores/`. Do not duplicate it per page.
  `studio` owns the catalog, connection/GPU polling, session clips and Speak selection;
  `player` owns the single docked player and waveform peaks.
- Keep polling in the layout (start/stop on mount/destroy), not in each page.

## Dependencies

- Pin exact versions (`bun add --exact`), never ranges; commit `bun.lock`.
- New direct dependencies need an exact version, a licence, a publication date and an
  audit entry in `docs/frontend/phase0-research.md`. Prefer releases at least 14 days
  old; Bun's `minimumReleaseAge` enforces it.
- Do not add a telemetry, analytics, external-font or CDN dependency. Do not add
  `sonner`, charts, the shadcn `sidebar` bundle, or another UI library for a preview.
- The waveform is SVG + Web Audio. Do not add wavesurfer or a chart library.
- Do not add install scripts. `bunfig.toml` sets `ignoreScripts = true`; native
  packages in this stack use optional platform packages, not lifecycle builds.
- Run `bun audit --audit-level=high`; there is no "fix later" for high/critical.

## Verification before claiming done

From `frontend/`:

```bash
bun run check             # svelte-check + scoped tsc
bun run check:types       # scoped tsc alone
bun run check:svelte5     # rune/Svelte 4 blocklist
bun run lint              # prettier --check + eslint
bun run test              # Vitest
bun run build
bun run check:spa         # static /ui/ fallback contract
bun run test:e2e          # starts an isolated mock daemon + Vite
bun run check:api
bun audit --audit-level=high
```

`bun run check` runs `svelte-check` and a scoped `tsc -p tsconfig.tsc.json`.
`tsc` cannot model named exports from generated shadcn `.svelte` primitives, so those
generated UI modules are excluded from the TS-only config and remain covered by
`svelte-check`. Do not remove the scoped tsc gate or add `any` to make it pass.

`bun run test:e2e` starts a daemon on `127.0.0.1:8765` with
`AGENT_VOICE_DATA_DIR=/tmp/agent-voice-qa-data` and the real plugin manifests from
`engines/`; no user voices or GPU state are touched. It needs `uv` and `ffmpeg`.
Screenshots are written to `/tmp/agent-voice-qa/`, never the repo.

Anything not actually run must be labelled `UNVERIFIED` with the exact reason.
