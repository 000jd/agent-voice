# agent-voice Studio — design system

Status: **implemented visual specification** for the preview UI. Reference frames were
downloaded from the actual Dribbble shots into `/tmp/mucer-ref/` for inspection only; no
Dribbble or Mucer asset is committed to this repository.

## Reference evidence

- Shot 1: [Mucer - AI Voice Platform](https://dribbble.com/shots/27550536-Mucer-AI-Voice-Platform)
  — obsidian studio, editorial composer, right history panel, waveform transport.
- Shot 2: [Mucer - AI Voice Platform - Voice Models](https://dribbble.com/shots/27593021-Mucer-AI-Voice-Platform-Voice-Models)
  — a denser rebound: table rows, compact filter chips, cool-navy data accents.
- Direct Dribbble requests returned 504 from this host. The same pages and full-resolution frames
  were reached through `r.jina.ai`; the published palettes and frame metadata were used to verify
  the token set. The pixel pass was done from the downloaded frames.

Published palettes:

| Shot | Tags |
|---|---|
| 1 | `#060606`, `#5B5B5B`, `#121550`, `#1721EF`, `#F1F2F4`, `#9B9EBB`, `#1B23A6`, `#DBA72F` |
| 2 | `#040608`, `#0E2B49`, `#5C6061`, `#EEEFEF`, `#9FA49F`, `#2A4863`, `#2143CE`, `#87777D` |

Reconciliation: Shot 1 owns the shell and action language. Shot 2 contributes the dense row
rhythm and `#2143CE` as a secondary data/selection accent. `#121550` is not a gradient; it may be
a 6–10% tint behind an active waveform/selection. Do not turn the app navy.

## What the pixels actually show

- A near-black canvas with very quiet surface separation: 1px hairlines, not shadowed cards.
- Cobalt is scarce: primary CTA, play controls, the progress/playhead, selected small accents.
- Shot 2 is a **table**, not a grid of large cards. Voice rows are ~54–60px tall with a small
  avatar, a 13–14px name, a 11–12px description, and muted metadata columns.
- Filter chips are compact and rectangular (8px radius); they are not oversized pills.
- Shot 1’s content column is editorial: large 30–34px lines, dimmed inactive text, one bright
  active line, a large quiet text entry surface, and a right panel of small history cards.
- The bottom transport in Shot 1 is a multi-track timeline; this app deliberately uses a single
  docked player inside the content column, leaving the rail visible.
- Typography is sans-serif, tightly set, with few weights; timestamps are the only numeric microtype.
- No decorative illustrations, no emoji labels, no glowing orbs, no stock avatars.

## Color tokens

Dark is the default. `--canvas` is the page; `--panel` is the rail/top/bottom player; `--elevated`
is a card or input; `--elevated-hover` is hover/selected.

| Token | Dark | Light | Use |
|---|---|---|---|
| `--canvas` | `#060606` | `#FAFAFB` | page |
| `--panel` | `#0B0B0E` | `#FFFFFF` | rail, top bar, player |
| `--elevated` | `#111116` | `#FFFFFF` | cards, inputs |
| `--elevated-hover` | `#16161D` | `#F1F2F4` | hover/selected rows |
| `--hairline` | `rgba(255,255,255,0.08)` | `#E4E5EA` | every border |
| `--focus` | `rgba(23,33,239,0.55)` | `rgba(23,33,239,0.40)` | focus border |
| `--accent` | `#1721EF` | `#1721EF` | primary action |
| `--accent-data` | `#2143CE` | `#2143CE` | secondary data/selection |
| `--accent-hover` | `#1B23A6` | `#1B23A6` | primary hover |
| `--text-primary` | `#F1F2F4` | `#111116` | body/headings |
| `--text-secondary` | `#9B9EBB` | `#5B5B5B` | descriptions, metadata |
| `--text-muted` | `#5B5B5B` | `#8A8A93` | labels, disabled, dividers |
| `--warning` | `#DBA72F` | `#A66A08` | cold start/warning |
| `--success` | `#22C55E` | `#15803D` | live/ok |
| `--danger` | `#EF4444` | `#DC2626` | error/destructive |

Additional tints:
- `--selected-tint: rgba(18,21,80,0.22)` dark / `rgba(23,33,239,0.06)` light.
- `--wave-unplayed: #2A4863` at 55% on dark; `#A8B4C4` at 60% on light.
- `--code-blue: #0E2B49` is a data accent only; never a page background.

Shadcn mapping is direct: `background/foreground`, `card`, `popover`, `primary`,
`secondary`, `muted`, `accent`, `destructive`, `border`, `input`, `ring`, `sidebar*`.
Buttons use 8px radius; cards/panels 12px; drawer 16px top-left radius only where it meets the
viewport; circular controls only for play and 28–32px monogram avatars.

## Typography

- Sans: **Inter Variable** (already self-hosted via `@fontsource-variable/inter`).
- Mono: `"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace`.
  No extra font package; system fallback is intentional.
- Weights: 400 regular, 500 medium, 600 semibold. No 700+ display weight.
- `font-variant-numeric: tabular-nums` is global; every timestamp, duration, VRAM figure, RTF,
  seed, character counter and byte size also uses the mono stack and `tabular-nums`.
- Scale:
  - Page title 22/28 semibold, tracking `-0.015em`.
  - Section title 15/20 semibold.
  - Body 14/20; small 12/16; micro 10/14 uppercase tracking `0.08em` (group labels only).
  - Editorial composer 22–26/1.45 on desktop; 18–20 mobile.
  - All captions and table headers 11–12px, `--text-secondary` or `--text-muted`.
- No letter-spaced body copy, no serif, no emoji in labels/buttons/statuses.

## Geometry and density

| Element | Measure |
|---|---|
| Rail | 236px expanded / 64px collapsed; starts below the full-width top bar |
| Top bar | 52px, full width: brand, Studio/Explore/System, search, primary action, connection, theme |
| Content | 24px mobile, 32px desktop; 1440px max for prose, full width for studio |
| Speak inspector | 320px sticky; stacks under 1280px |
| Drawer | 420px desktop / full width mobile; 16px edge radius; overlay `#060606/70` |
| Buttons | 32px default, 36px primary; 8px radius; 10–12px horizontal padding |
| Inputs/selects | 32–36px; 8px radius; 1px hairline; 12px text |
| Cards | 12px radius; 16px padding; 12px internal gaps; 1px hairline |
| Voice rows | 56–64px; avatar 28px; 1px row divider; hover elevated-hover |
| Player | ~132px desktop, two-tier (controls + time ruler/waveform), left edge aligned to content; compact on mobile; 1px top hairline |
| Waveform | 96 bars; 3–5px wide; 1–2px gap; 28–36px tall |
| Focus | 1px accent border + 3px tinted ring; never removed |
| Motion | 140–180ms ease-out; no bounce, no glow; reduced-motion respected |

## Component rules

- **Primary button:** cobalt surface, white text, hover `#1B23A6`; no gradient.
- **Secondary:** elevated surface + hairline; hover elevated-hover.
- **Ghost:** text-only; hover subtle surface; used for icon controls.
- **Inputs/selects/textarea:** `#111116` dark / `#FFFFFF` light; placeholder `--text-muted`;
  focus ring as above. Search fields use a leading icon and 8px radius.
- **Filter chips:** 32px height, 8px radius, 1px hairline; active uses `--selected-tint` and a
  cobalt label. Source/status badges use rounded-full and tinted borders.
- **Segmented control:** one 1px container, equal segments, 32px height, active elevated-hover;
  keyboard arrow support.
- **Switch:** 28×16 track, 12px thumb, cobalt on; 44px touch wrapper.
- **Range:** 4px track, 12px cobalt thumb; value in mono tabular.
- **Cards:** one object per card; no fake KPI grids, no sparklines. Cards are separated by value
  and hairlines, not heavy shadows.
- **Voice library:** one bordered table surface with divided rows, not a stack of cards. Avatar monogram (flat
  deterministic cobalt/navy/steel tone), name + source chip, 12px description, duration/date,
  Use in Speak and delete. Built-in presets show engine/model/speaker; no invented duration.
- **Speaker wall:** when a preset model has many speakers, use a compact select or a 2-column
  chip list, never a wall of equal giant cards.
- **Drawers:** title + one-line purpose; scroll body; sticky footer; Esc closes; focus returns to
  trigger. Design/clone drawers are 420px.
- **Player:** clip title + engine/model mono; waveform scrub; play/pause; current/total time;
  speed 0.75–1.5; volume; fetch-based download; close. Error is an inline line, not a toast.
- **Waveform:** real decoded peaks, role slider, pointer drag + arrow keys; played cobalt,
  unplayed navy/steel. No fake bars.
- **Empty states:** one muted icon tile, one sentence, one action; no illustration.
- **Tables/lists:** 1px dividers, muted headers, hover row, sticky-ish filter row on wide screens.

## Anti-"AI slop" rules

1. No purple-pink-cyan glowing gradients, neon drop shadows, blurred glass, or pulsing orbs.
2. No emojis in UI labels, buttons, statuses or toasts.
3. No 4-column vanity KPI cards, fake sparklines or invented metrics.
4. Every border is a crisp 1px hairline; no thick decorative borders except focus rings.
5. Tight intentional padding; no giant empty hero areas in the studio.
6. Color is reserved for action, state and data. Quiet by default.
7. Do not invent languages, accents, categories, usage counts or voice durations. The API does
   not expose them.
8. No external images, stock avatars, Dribbble assets or CDN requests.
9. Dark is default; light is the same geometry, not a different design.
10. Numbers that can change use tabular numerals; no layout jitter.

## Responsive behavior

- `>=1280px`: rail + center + 300px inspector; player fixed.
- `768–1279px`: rail; inspector below composer; player fixed.
- `<768px`: top bar + menu sheet; no rail; stacked inspector; compact player; 40px touch targets;
  no horizontal page scroll.
- Long text wraps; metadata truncates with a tooltip/title; no clipped controls.

## Accessibility

- Focus-visible everywhere; drawers trap focus and restore; Esc closes; mobile menu closes on
  route change.
- Semantic headings, labels, `aria-current` on nav, `aria-live` for generation/transcription and
  player errors, `role="slider"` waveform with `aria-valuetext`.
- Contrast check: `#9B9EBB` is the smallest body tone; `#5B5B5B` is only disabled/labels/icons.
  Light mode keeps body text at `#5B5B5B` or darker.
- Reduced-motion media query disables slide/fade motion.
- The player is keyboard operable and does not steal Space from inputs/textareas/contenteditable.

## Screen compositions

- **Shell:** full-width studio bar; rail and content below; player docked in the content column. Mobile menu replaces the rail.
- **Speak:** editorial composer + inspector; underline Setup/Session tabs; primary generate in composer; no dashboard-style page title.
- **Voices:** one dense table panel with search/filter, design/clone drawers; built-in presets
  are a separate filter.
- **Models:** grouped TTS/ASR engine cards; model rows with real installed/downloaded/loaded/VRAM.
- **Transcribe:** drag-drop upload, ASR controls, transcript + timed segments, SRT/VTT downloads.
- **Dashboard:** editorial “start a take” hero, a quiet system panel, and recent session renders; no KPI tile grid.
- **History:** session-local only; no fake durable history.
- **Settings:** read-only runtime/security settings, theme, audio-key explanation.

## API honesty notes

- No REST `server_info`: clone drawer shows `<DATA_DIR>/inbox/` and the `agent-voice doctor` hint.
- Single design render has no URL: audition 2–3, then save the chosen take with `take`/`seed`.
- Saved voices have no preview endpoint; only design candidates are playable.
- Audio is fetched with `fetch` + object URL; a 401 from a keyed daemon shows the daemon-key
  explanation, not a fake player.
- Inline transcription segments can be truncated; show `segments_omitted` and the daemon JSON path.

## QA checklist

- [ ] 1440×900 and 390×844 screenshots for every screen, dark.
- [ ] light dashboard/Speak/voices parity.
- [ ] no horizontal overflow, no clipped buttons, no overlap with the player.
- [ ] cobalt only on action/playhead/selection; surfaces are obsidian, not slate.
- [ ] all timestamps/VRAM/RTF/char counters tabular.
- [ ] empty, loading, error and 401 states are legible.
- [ ] keyboard: nav, segmented control, switch, slider, drawer, player.
- [ ] no external requests besides same-origin daemon in production.
