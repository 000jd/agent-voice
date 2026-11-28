# agent-voice Studio — Graphite & Brass

Status: **implemented original visual system** for the v0.7 Studio. No Dribbble,
Mucer, VoiceStudio or Voicebox layout, palette, copy or asset is used.

## Identity

Warm graphite surfaces, bone-paper light mode, one brass accent, precise hairlines
and dense studio typography. The interface should feel like a working desk, not a
marketing page: quiet surfaces, clear labels, real data, one obvious action.

## Tokens

| Token | Dark | Light | Use |
|---|---|---|---|
| `--canvas` | `#11100E` | `#F4EFE6` | page |
| `--panel` | `#171612` | `#FBF7F0` | rail, top bar, inspector |
| `--surface` | `#1D1B16` | `#FFFFFF` | cards, inputs, rows |
| `--surface-hover` | `#26231C` | `#F1EAE0` | hover/selected |
| `--rule` | `#332F27` | `#DED3C4` | 1px hairline |
| `--rule-strong` | `#4A4439` | `#C2B5A3` | stronger divider |
| `--text` | `#F3EEE4` | `#1B1915` | body/headings |
| `--text-secondary` | `#B8AEA0` | `#63594D` | descriptions |
| `--text-muted` | `#8C8173` | `#6F6458` | labels |
| `--signal` | `#D8A657` | `#8A5A00` | the one accent |
| `--signal-strong` | `#B9822E` | `#6E4700` | hover/pressed |
| `--on-signal` | `#1A140A` | `#FFF8EA` | text on brass |
| `--signal-soft` | `rgba(216,166,87,.14)` | `rgba(138,90,0,.10)` | selection |
| `--success` | `#8CB89A` | `#2F6B46` | ok |
| `--danger` | `#E07A6A` | `#B3261E` | error/stop |
| `--warning` | `#E0A94A` | `#8A5A00` | warnings |
| `--wave-idle` | `#4A4439` | `#C2B5A3` | unplayed waveform |

Dark is the default. Light is a complete token swap with the same geometry.

## Type

- Inter Variable, self-hosted through `@fontsource-variable/inter`.
- System monospace for metrics and code-like values.
- Minimum readable text 12px; body 14/20; page title 20/26; section 14/20.
- Weights 400/500/600. No display weights.
- `font-variant-numeric: tabular-nums` globally; every duration, timestamp, seed,
  byte, VRAM figure and counter uses the mono stack.

## Geometry

- 4/8px spacing grid; 6px controls; 10px panels; 1px rules.
- 224px expanded / 64px collapsed rail; 56px top bar; player inside the content
  column.
- No radii above 10px except a pill status; no shadows, glass, gradients or glows.
- Brass is reserved for primary action, focus, selection and playhead. State colours
  are semantic, never decoration.

## Components

- Primary button: brass surface, `--on-signal` text, brass-strong hover.
- Secondary: surface + rule; ghost: transparent with a quiet hover.
- Inputs/selects/textareas: surface, 1px rule, focus-visible brass ring.
- Chips: 24–28px, 999px only for state/source; no oversized pills.
- Cards: surface + 1px rule, 16px padding; voice cards are a grid of objects, not a
  table; presets are one card per speaker.
- Voice avatar: flat deterministic monogram tile, never a gradient blob or stock face.
- Waveform: real decoded/server peaks only; played brass, unplayed `--wave-idle`.
- Player: real waveform, seek, rate, volume, download, close; keyboard operable.
- Empty states: one sentence, one action, no illustration; they teach the next step.
- Dialogs/sheets: focus trap, Esc, restore focus, sticky footer, full-height on mobile.

## Interaction

- Hover, focus-visible, active, disabled and loading on every control.
- Minimum 24px hit target; 44px on touch. Keyboard alternatives for drag, and
  numeric fields beside trim handles.
- Toasts are `role="status"`; errors stay inline next to the failing control.
- `prefers-reduced-motion` removes non-essential motion.
- No dead space: screens fill the viewport; panes scroll independently.
- No marketing copy inside the tool.

## Anti-slop checklist

No purple/pink/cyan gradients, neon shadows, blurred glass, pulsing orbs, emoji
labels, fake KPIs, fake sparklines, invented language/gender/usage/duration or
fabricated waveform bars. No external images, stock avatars or CDN requests.

## Accessibility

WCAG AA: 4.5:1 text, 3:1 UI/focus, visible 2px focus ring, 24px targets, reflow at
320px. Axe must report zero serious/critical violations on every screen/state.
