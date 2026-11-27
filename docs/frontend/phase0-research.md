# Frontend Phase 0 — research report

**Retrieved:** 2026-11-27
**Scope:** a blank `frontend/` for the existing `agent-voice` daemon. No backend,
engine, installer or Python behaviour changes. No version bump.
**Runtime used for probes:** Node 24.22.3 (nvm, user-level), Bun 1.4.5, Python 3.12
via `uv`, Linux x86_64. GPU not involved.

This report records the research and the **measured** probe results. Anything not
run is marked **UNVERIFIED**; do not promote a plan to a fact without a probe.

---

## R1 — Svelte 5

Sources: `https://svelte.dev/docs/svelte/` (runes, snippets, event attributes,
testing), `https://registry.npmjs.org/svelte`, `https://svelte.dev/docs/svelte/testing`.

- Stable at retrieval: **svelte@5.59.2** (MIT, published 2026-11-25).
- Runes are the only state model used here: `$state`, `$derived`/`$derived.by`,
  `$effect`, `$props`, `$bindable`. Snippets replace slots; `{@render}` calls them.
- Event attributes replace directives: `onclick`, not `on:click`. Callback props
  replace `createEventDispatcher`.
- `svelte-check` only reports legacy syntax when a component is in runes mode. The
  scaffold therefore combines `svelte-check --fail-on-warnings` with
  `frontend/scripts/check-svelte5.mjs`, a line-level blocklist.
- Blocklist enforced: `export let`, `$$props`, `$$restProps`, `$:`, `on:*`, `<slot>`,
  slot attributes, `let:`, `createEventDispatcher`, `new Component({ target })`,
  `<svelte:component>`, `<svelte:self>`, `beforeUpdate`/`afterUpdate`, `$app/stores`,
  `$lib`, and `{@const}` as a project convention.
- A component-local variable named `state` collided with `$state` in `svelte-check`
  (it parsed `$state` as a store auto-subscription). The scaffold uses `view`; this is
  recorded in `frontend/AGENTS.md`.
- `svelte-check` is the type gate. `tsc --noEmit` cannot model named exports from
  `.svelte` files (it failed on generated shadcn `button/index.ts`), so it is not a
  gate.

## R2 — SvelteKit 3 static SPA

Sources: `https://svelte.dev/docs/kit/`, `https://svelte.dev/docs/kit/adapters`,
`https://svelte.dev/docs/kit/single-page-apps`,
`https://registry.npmjs.org/@sveltejs/kit`, `https://registry.npmjs.org/@sveltejs/adapter-static`,
`https://registry.npmjs.org/@sveltejs/vite-plugin-svelte`, `https://registry.npmjs.org/vite`.

- SvelteKit is on **v3**. Retrieved: `@sveltejs/kit@3.4.1`, `adapter-static@4.0.1`,
  `vite-plugin-svelte@7.3.2`, `vite@8.4.0`, Node ≥22.17, TypeScript 6.
- **`svelte.config.js` is gone.** `sv create` (1.7.1) emitted no such file; all config
  is in `sveltekit()` inside `vite.config.ts`.
- Kit 3 renames: `$lib` → `#lib` (package `imports`), `$app/stores` → `$app/state`,
  `$app/environment` → `$app/env`, `$app/paths`'s `base`/`assets` → `resolve()`/
  `asset()`, `invalidateAll` → `refreshAll`, `json()`/`text()` → `Response` helpers.
  Route `Path` values are base-relative and have no leading slash (`''`, `'speak'`).
- Static SPA recipe: `adapter-static` with `fallback: '200.html'`,
  `paths: { base: '/ui', relative: false }`, and a root `+layout.ts` with
  `ssr = false`, `prerender = false`, `trailingSlash = 'never'`.
- Measured build: `build/200.html`, `build/_app/version.json`, hashed
  `build/_app/immutable/...`; **no** `build/index.html`; asset URLs are
  `/ui/_app/...`.
- The fallback contract was measured with a throwaway server: `/ui/` 200 HTML,
  `/ui/voices` (hard refresh) 200 HTML, missing `/ui/_app/*.js` 404, real
  `/ui/_app/version.json` 200 JSON. A host must not fall back `_app` misses to HTML.
- Chosen pins (all ≥14 days old at retrieval): `kit@3.2.0` (16.6d),
  `adapter-static@4.0.1` (28.0d), `vite-plugin-svelte@7.3.1` (64.6d),
  `vite@8.3.8` (15.9d), `svelte@5.57.3` (31.2d). This is the `sv` template's own
  baseline, not the newest patch.

## R3 — Tailwind v4, shadcn-svelte and Bits UI

Sources: `https://tailwindcss.com/docs/`, `https://shadcn-svelte.com/docs/`,
`https://registry.npmjs.org/tailwindcss`, `https://registry.npmjs.org/shadcn-svelte`,
`https://registry.npmjs.org/bits-ui`, `https://registry.npmjs.org/@lucide/svelte`,
`https://registry.npmjs.org/@fontsource-variable/inter`.

- **tailwindcss@4.3.5** + **@tailwindcss/vite@4.3.5**; CSS-first `@import "tailwindcss";`,
  `@theme inline`, `@custom-variant dark (&:is(.dark *));`; no `tailwind.config.js`.
- **shadcn-svelte@1.8.1** (15.1d; 1.8.2 was 1.5d) + **bits-ui@2.21.0** (15.9d).
  Presets are named `nova`, `vega`, `maia`, `lyra`, `mira`, `luma`, `sera`, `rhea`;
  `nova` with the neutral base produces the scaffold's card/button/input/tooltip.
- `components.json` aliases were set to `#lib`, `#lib/components`,
  `#lib/components/ui`, `#lib/hooks`, `#lib/utils`. Measured: the CLI generated
  `import { cn } from "#lib/utils.js"` in every primitive — no `$lib` leakage.
- `$lib/utils` is a re-export of **`cn`** from the standalone `cn` package; `clsx` and
  `tailwind-merge` are not installed by shadcn. `tailwind-variants@3.3.1` is the
  compatible v3 line (v4.1.0 is newer and not used). `tailwind-merge@3.7.0` is the
  peer.
- Icons: **`@lucide/svelte@0.561.0`** (ISC, 349.6d) — the version the shadcn registry
  targets. `lucide-svelte` is deprecated. `@lucide/svelte@1.82.0` was published the
  audit day and was deliberately not used.
- Font: **`@fontsource-variable/inter@5.3.0`** (OFL-1.1, 130.8d), imported from
  `app.css`; self-hosted, no Google Fonts/CDN. The scaffold overrides shadcn's Geist
  default with Inter.
- Dark mode is a hand-rolled `theme.svelte.ts` + a blocking script in `app.html` and a
  toggle. `mode-watcher` (517 days stale, single maintainer) and `sonner` are not
  installed for this preview.
- `bits-ui` ships Svelte sources; `vite.config.ts` sets `ssr.noExternal: ['bits-ui']`.
  Measured: `svelte-check` 0 errors and the production build succeeds with all four
  primitives.

## R4 — typed API client

Sources: `https://openapi-ts.dev/`, `https://registry.npmjs.org/openapi-typescript`,
`https://registry.npmjs.org/openapi-fetch`, `https://svelte.dev/docs/kit/observability`.

- `openapi-typescript@7.13.0` and `openapi-fetch@0.17.0` (both MIT, 288.3d). The
  generated schema is committed; the runtime client is small.
- `scripts/export_openapi.py` builds the app with a temp home/data/engines, empty
  engine registry, `mount_mcp=False`, and explicit settings, then writes
  `json.dumps(schema, indent=2, sort_keys=True) + "\n"`. Measured: 24 paths, 57 KB,
  byte-identical across runs; `/health` and `/v1/engines` present; `/v1/audio/ws`
  absent.
- Measured `openapi-typescript@7.13.0` generation under TypeScript 6 succeeds. Its
  declared peer is TypeScript `^5.x`; this is a documented waiver (generation-only
  tool, not runtime).
- TypeScript 6 + the generated schema exposed two client issues:
  1. the method-based `api.GET('/health')` inferred `never` (generic constraint);
  2. `/health` and `/v1/engines` have no response models, so their error branch is
     `never` and a naive `result.error` guard narrows to `never`.
  The studio uses openapi-fetch's **path-based** client
  (`createPathBasedClient<paths>`) and a narrow `RawResult` cast only at the
  unmodelled endpoints; `src/lib/api/guards.ts` parses the unknown payload with
  explicit guards. The backend follow-up is to add `response_model` to those routes.
- Browser audio: `/audio/<id>` is bearer-protected. `<audio src>` cannot send
  `Authorization`; the player fetches the blob, plays `URL.createObjectURL`, and
  shows a clear 401 when the daemon has a key the static UI cannot store.
- SSE uses `fetch` + `eventsource-parser` (EventSource cannot set headers). WebSocket
  auth uses `Sec-WebSocket-Protocol: bearer, <key>`. Neither is implemented here.

## R5 — tooling, tests and package manager

Sources: `https://bun.com/docs/`, `https://vitest.dev/`, `https://playwright.dev/`,
`https://eslint.org/`, `https://svelte.dev/docs/kit/`, npm registry packuments.

- **Bun 1.4.5** is the sole PM. `bun.lock` is committed; `bun ci` uses it frozen.
  Bun runs scripts via Node shebangs; never `--bun` for Vite/Vitest/Playwright/ESLint.
- **Node 24.22.3** (nvm) is required. Node 23.4.0 is outside every current engine
  range. `package.json` pins `>=24.0.0 <25`; CI asserts the major because Bun ignores
  `engines`.
- **TypeScript 6.0.3** (`~6.0.3`): Kit 3 peers `^6`, svelte-check peers `^5 || ^6`, and
  typescript-eslint peers `<6.1`; TypeScript 7 is incompatible with all three.
- Unit/component tests: **Vitest 5.1.0 + happy-dom 21.0.4 +
  @testing-library/svelte 5.4.2 + @testing-library/jest-dom 7.0.1**. This is the
  browser-free path; measured 4 tests pass. The official `sv` browser mode
  (`@vitest/browser-playwright` + `vitest-browser-svelte`) is documented as the
  upgrade for layout/focus tests, not used here.
- Browser tests: **@playwright/test 1.65.0** (21.2d). Measured: Chromium headless
  shell downloaded and launched without sudo on this host (Playwright warns the OS is
  not officially supported; the fallback build works). Three smoke tests passed at
  the scaffold gate: every route, dark-mode toggle/persistence, and the dashboard
  error state. The studio preview later added live Speak/voice/transcription and
  visual E2E; see `docs/frontend/architecture.md`.
- Lint/format: **ESLint 10.14.0** + `eslint-plugin-svelte@3.24.1` +
  `typescript-eslint@8.72.2`; **Prettier 3.10.1** + `prettier-plugin-svelte@4.1.1` +
  `prettier-plugin-tailwindcss@0.8.1`. Measured clean. The generated shadcn primitives
  get two narrow rule exceptions (`no-navigation-without-resolve`,
  `no-unsafe-assignment`); `NavLink` gets the navigation exception because it receives
  an already-resolved href.
- `svelte-check@4.7.8` with `--fail-on-warnings`; `svelte-kit sync` runs in
  `prepare` and at the start of `check`/`build`.
- `bunfig.toml`: `minimumReleaseAge = 1209600` (14 days), `ignoreScripts = true`.
  Every direct pin cleared the gate on this run; no allowlist was needed.

## R6 — repository, guard and packaging

Sources: `src/agent_voice/api/guard.py`, `src/agent_voice/api/app.py`,
`src/agent_voice/config.py`, `pyproject.toml`, `.gitignore`, `.github/workflows/ci.yml`.

- Guard: Host is canonicalised (port dropped); Origin hostname must be loopback
  (`127.0.0.1`, `localhost`, `::1`); Bearer applies to `/v1`, `/audio` and `/mcp`;
  `/health`, `/docs`, `/openapi` are exempt. No CORS middleware.
- Dev proxy: `/health`, `/v1`, `/audio` → `http://127.0.0.1:8000`, `changeOrigin`,
  `Origin: http://127.0.0.1:8000`, optional `Authorization: Bearer` from
  `AGENT_VOICE_DEV_API_KEY` (read via `loadEnv`/`process.env`, never `VITE_*` or
  `PUBLIC_*`), `ws: true` for `/v1`. Measured: proxy `/health` and `/v1/engines` 200
  with a key; direct `/v1/engines` 401; direct `Origin: http://evil.example` 403.
- `/ui` is not bearer-protected (static assets cannot carry a key), but Host/Origin
  still apply. The future mount must 404 `/ui/_app/*` and fall back only other
  `/ui/*`; `api/app.py`'s `StarletteHTTPException` handler rewrites every 404 to JSON,
  so the fallback must be an explicit route/subclass, not the default 404 handler.
- Packaging: wheel `packages = ["src/agent_voice"]`; sdist now excludes `frontend`;
  Ruff `extend-exclude = ["frontend"]`. No Python dependency/version change.
- `.gitignore`: root `*.lock` needed `!frontend/bun.lock`; build output stays ignored.
- Dist shipping decision: this scaffold does **not** commit `frontend/build`. The
  future UI-readiness release builds it in CI and copies it into package data
  (`src/agent_voice/web/`) in the release commit, so a tag clone + `uv sync` carries a
  UI with no Node/Bun. That is a backend change and is out of scope here.
- CI: a separate `frontend-ci.yml` runs Node 24 + Bun, `bun ci`, check, blocklist,
  lint, unit tests, build, Playwright, and audit; a second job runs the OpenAPI drift
  check with `uv`. The existing Python CI is untouched.

## R7 — supply chain, ages and licences (retrieved 2026-11-27)

Sources: `https://registry.npmjs.org/<pkg>`, `https://api.npmjs.org/downloads/point/last-week/<pkg>`,
published tarballs; `https://pnpm.io/cli/audit`, `https://bun.com/docs/pm/cli/audit`,
`https://docs.npmjs.com/cli/v11/commands/npm-audit`.

**Chosen direct versions** (all published ≥14 days before 2026-11-27):

| package | version | published | age | licence |
|---|---|---|---|---|
| svelte | 5.57.3 | 2026-10-26 | 31.2d | MIT |
| @sveltejs/kit | 3.2.0 | 2026-11-10 | 16.6d | MIT |
| @sveltejs/adapter-static | 4.0.1 | 2026-10-29 | 28.0d | MIT |
| @sveltejs/vite-plugin-svelte | 7.3.1 | 2026-09-23 | 64.6d | MIT |
| vite | 8.3.8 | 2026-11-11 | 15.9d | MIT |
| typescript | 6.0.3 | 2026-04-16 | 224.0d | Apache-2.0 |
| tailwindcss | 4.3.5 | 2026-11-06 | 20.4d | MIT |
| @tailwindcss/vite | 4.3.5 | 2026-11-06 | 20.4d | MIT |
| shadcn-svelte | 1.8.1 | 2026-11-11 | 15.1d | MIT |
| bits-ui | 2.21.0 | 2026-11-11 | 15.9d | MIT |
| tailwind-variants | 3.3.1 | 2026-08-03 | 115.4d | MIT |
| tailwind-merge | 3.7.0 | 2026-09-12 | 75.2d | MIT |
| cn | 0.5.2 | 2026-11-02 | 24.7d | MIT |
| tw-animate-css | 1.4.0 | 2025-09-24 | 428.8d | MIT |
| @internationalized/date | 3.12.4 | 2026-09-01 | 86.4d | Apache-2.0 |
| @lucide/svelte | 0.561.0 | 2025-12-12 | 349.6d | ISC |
| @fontsource-variable/inter | 5.3.0 | 2026-07-19 | 130.8d | OFL-1.1 |
| openapi-typescript | 7.13.0 | 2026-02-11 | 288.3d | MIT |
| openapi-fetch | 0.17.0 | 2026-02-11 | 288.3d | MIT |
| vitest | 5.1.0 | 2026-11-02 | 24.3d | MIT |
| @testing-library/svelte | 5.4.2 | 2026-06-23 | 156.9d | MIT |
| @testing-library/jest-dom | 7.0.1 | 2026-08-09 | 109.0d | MIT |
| happy-dom | 21.0.4 | 2026-10-20 | 37.2d | MIT |
| @playwright/test | 1.65.0 | 2026-11-05 | 21.2d | Apache-2.0 |
| eslint | 10.14.0 | 2026-10-30 | 27.4d | MIT |
| @eslint/js | 10.0.1 | 2026-02-06 | 293.1d | MIT |
| eslint-plugin-svelte | 3.24.1 | 2026-11-07 | 19.6d | MIT |
| typescript-eslint | 8.72.2 | 2026-11-09 | 17.3d | MIT |
| globals | 17.14.0 | 2026-11-01 | 25.5d | MIT |
| eslint-config-prettier | 10.1.8 | 2025-07-18 | 496.2d | MIT |
| prettier | 3.10.1 | 2026-11-12 | 14.7d | MIT |
| prettier-plugin-svelte | 4.1.1 | 2026-06-15 | 164.3d | MIT |
| prettier-plugin-tailwindcss | 0.8.1 | 2026-07-15 | 134.5d | MIT |
| svelte-check | 4.7.8 | 2026-11-01 | 25.2d | MIT |
| @types/node | 24.19.2 | 2026-10-09 | 48.2d | MIT |

Peers/waivers:

- `openapi-typescript@7.13.0` declares TypeScript `^5.x`; the project requires TS 6
  for Kit 3. The CLI is generation-only; the waiver is deliberate and re-checked by
  `bun run check:api` on every CI run.
- `@lucide/svelte` is deliberately older than the latest (`1.82.0` was published on
  the audit day); `0.561.0` is the shadcn registry target.
- `@testing-library/jest-dom@7.0.1` peers `@testing-library/dom >=10 <11`, which is
  the version pulled by Testing Library Svelte 5.4.2.

Licence flags:

- **MPL-2.0** `lightningcss` is a transitive dependency of Vite 8 / Tailwind. It is an
  unmodified dependency; do not vendor or patch it without review.
- **OFL-1.1** `@fontsource-variable/inter`: bundling/self-hosting is allowed; keep the
  notice and do not sell the font standalone.
- **null licence metadata** on `svelte-toolbelt` (transitive of bits-ui): the package
  contains an MIT LICENSE file by Hunter Johnston; a manual determination must be
  recorded upstream/pinned. It is not a runtime network dependency.
- No GPL/AGPL/SSPL in the direct set.
- Single-maintainer concentration: shadcn-svelte, bits-ui, mode-watcher,
  svelte-toolbelt, tailwind-variants, clsx, tailwind-merge. Pins + lockfile are the
  mitigation; this is a bus-factor note, not a trust verdict.

Install-time scripts and telemetry:

- No package in the chosen set declares `preinstall`/`install`/`postinstall`.
  `@tailwindcss/oxide`, `rolldown` and `lightningcss` ship platform packages, not
  install scripts. `bunfig.toml` sets `ignoreScripts = true`; Playwright browsers are
  an explicit `bunx playwright install`, never implicit.
- No telemetry in `sv`, shadcn-svelte, Vite, Vitest or Playwright (tarball grep).
  SvelteKit's `tracing.server` is off and must stay off; `DO_NOT_TRACK` is a no-op in
  these tools today, so do not document it as a control.
- Blocking gate: `bun audit --audit-level=high` (measured "No vulnerabilities found"
  on the probe tree). Moderate is report-only; a frontend dev tree generates
  unactionable low/moderate noise.

## R8 — future libraries (research only; nothing installed)

- **wavesurfer.js@8.1.1** (BSD-3-Clause) for waveform playback; **uplot@1.6.32** (MIT)
  for timelines. Both lazy-load, same-origin, no telemetry.
- `wavesurfer-multitrack@0.4.12` hard-depends on wavesurfer 7, so it cannot be used
  with v8. A future multitrack view uses N single-track instances or stays on v7.
- Recording: `MediaRecorder` + `OfflineAudioContext` + `audiobuffer-to-wav`; playback
  must fetch `/audio/<id>` with the bearer token and use an object URL.
- No chart, player or recorder dependency is in `package.json` today.

---

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Framework | Svelte 5 + SvelteKit 3 | Owner requirement; current stable; runes. |
| Rendering | Static SPA, `ssr=false`, fallback `200.html` | UI is a local REST client; no Node runtime; daemon stays headless. |
| Base path | `/ui`, `paths.relative=false` | Future daemon mount; assets are absolute `/ui/_app/...`. |
| Config location | `vite.config.ts` only | Kit 3 removed `svelte.config.js`. |
| Imports | `#lib` | Kit 3 replacement for `$lib`. |
| Package manager | Bun 1.4.5 + `bun.lock` | Owner preference; exact pins; no dependency scripts. |
| Runtime | Node 24.22.3 | Node 23 is outside every current peer range; user approved nvm. |
| TypeScript | 6.0.3 | Kit 3 peer; TS 7 breaks Kit/svelte-check/typescript-eslint. |
| CSS | Tailwind 4.3.5 | CSS-first, Vite plugin, no config file. |
| Components | shadcn-svelte 1.8.1 + bits-ui 2.21.0 | Minimal primitives; `#lib` aliases verified. |
| Font | Inter variable, self-hosted | Zero external requests; OFL-1.1. |
| Icons | `@lucide/svelte@0.561.0` | shadcn target; avoids the same-day 1.82.0. |
| Dark mode | Hand-rolled theme + inline pre-paint script | Avoids stale mode-watcher; no FOUC. |
| Unit tests | Vitest 5 + happy-dom + Testing Library | Browser-free gate; 4 tests passed. |
| E2E | Playwright 1.65.0 + Chromium shell | Live mock-daemon suite: navigation, playback, design/save, transcription and visual captures; CI uses `--with-deps`. |
| API types | openapi-typescript 7.13 + openapi-fetch 0.17 path-based | Generated schema; method form infers `never` under TS 6. |
| Dev auth | Vite proxy rewrites Origin + injects key | Guard unchanged; key never public. |
| Dist | Not committed in this scaffold | UI-readiness release builds in CI and ships package data; no Node at install. |
| CI | Separate `frontend-ci.yml` | Do not disturb Python CI surfaces. |

### Age-gate exceptions

None in the direct set: every chosen version is ≥14 days old. Bun's
`minimumReleaseAge = 1209600` and `ignoreScripts = true` are active. The two
non-age waivers are `openapi-typescript`'s TS `^5` peer and the deliberate use of
`@lucide/svelte@0.561.0` over its newer release.

### Measured probe log

| Probe | Result |
|---|---|
| Node install via nvm | `v24.22.3`; Bun `1.4.5` |
| `sv create` Kit 3 minimal | no `svelte.config.js`; `#lib`; `$app/tsconfig` |
| static SPA build | `build/200.html`; no `index.html`; `/ui/_app` assets |
| fallback server | `/ui/` 200; `/ui/voices` 200; `_app` miss 404; asset 200 |
| shadcn 1.8.1 add | `button`, `card`, `input`, `tooltip`; all imports `#lib/utils.js` |
| `svelte-check` | 0 errors / 0 warnings |
| production build with bits-ui | success |
| Vitest happy-dom | 4 tests passed |
| Playwright Chromium | launched; 3 tests passed |
| OpenAPI exporter | 24 paths, 57 KB, byte-identical twice |
| openapi-typescript under TS 6 | generated 1,854-line `schema.d.ts` |
| ESLint 10 + Prettier | clean |
| live daemon + Vite proxy | `/health` 200, `/v1/engines` 200 with key; direct 401; evil Origin 403 |
| clean `bun ci` | frozen install from a copy with no `node_modules`/build; check and build pass |
| `uv build` | wheel clean; sdist excludes frontend source (only `docs/frontend/*` docs remain) |
| audit (final tree) | no high/critical; one advisory below the high threshold (298 packages) |

## UNVERIFIED

- CI execution on GitHub-hosted runners (the workflow is committed, not run here).
- Playwright on macOS/Windows and on browsers other than Chromium.
- Future npm advisories and future releases; re-run `bun audit` and the age check.
- The `openapi-typescript` TS 6 peer waiver remains a warning; no upstream 8.x exists.
- The daemon `/ui` mount and package-data distribution are not implemented here.
- The browser bundle was not profiled on a low-end device; this is a preview
  scaffold, not a performance claim.
