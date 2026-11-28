# Phase 0 studio research

Status: research complete for v0.7.0. All sources retrieved **2026-11-28**. This
report records workflow and information-architecture findings only. No code, assets,
icons, screenshots or copy were copied from any product below.

## Method and limits

- Read-only inspection of public repositories, documentation and standards.
- No repository was cloned, installed or executed; no product screenshot was copied.
- VoiceStudio and Voicebox screenshots are binary WebP assets that the read-only fetch
  path could not decode. Their layout notes below are from README/docs structure, not
  pixel measurement. This is not a blocker: v0.7.0 deliberately uses an original visual
  system rather than reproducing a reference.
- The current backend inventory was read directly from this checkout at
  `dbf7832`; file/line claims are in section 6.

## 1. VoiceStudio (`debpalash/VoiceStudio`, AGPL-3.0)

Sources:

- <https://github.com/debpalash/VoiceStudio>
- <https://raw.githubusercontent.com/debpalash/VoiceStudio/main/docs/audio-quality.md>
- <https://raw.githubusercontent.com/debpalash/VoiceStudio/main/docs/feature-catalog.md>
- <https://raw.githubusercontent.com/debpalash/VoiceStudio/main/docs/electron-voice-design.md>
- <https://raw.githubusercontent.com/debpalash/VoiceStudio/main/docs/speech-platform.md>

Useful workflow ideas:

- One workspace per job: voice cloning, voice design, dubbing, dictation,
  stories/audiobooks/batch, model catalogue.
- First-run path: pick an existing voice or add a clean reference recording; the
  documentation presents a short clean sample as the starting point and longer as
  better, but that is product guidance rather than a standard.
- Model management separates engine, device, install state and licence notices, and
  asks before downloading a model.
- Audio quality controls are separate from model quality; local signal checks
  (silence, clipping, empty audio) are advisory and do not claim to measure voice
  similarity.
- A status-bar engine switcher makes the active backend visible while working.

Licence warning: the app is AGPL-3.0 and its model weights have separate licences
(including non-commercial weights). No code or assets may be carried into this MIT
project. Only the workflow ideas above were used.

## 2. Voicebox (`jamiepine/voicebox`, MIT)

Sources:

- <https://github.com/jamiepine/voicebox>
- <https://docs.voicebox.sh/overview/creating-voice-profiles>
- <https://docs.voicebox.sh/overview/captures>
- <https://docs.voicebox.sh/overview/generation-history>
- <https://docs.voicebox.sh/overview/mcp-server>

Useful object-model ideas:

- Typed voice profiles: cloned, preset and designed voices share one selection surface
  but have different intake and engine constraints.
- A voice can be created from files or in-app recording; captures are a separate
  archive of input audio + transcript, not the same object as a reference sample.
- A generation is a stable parent with takes/versions. Retry keeps the seed; a
  creative reroll changes it and creates a new take. Provenance (engine, seed,
  effects, source) belongs with the take.
- A serial queue can be represented as row state plus progress instead of a separate
  queue screen.
- Compatibility is communicated by dimming and explaining, not by silently hiding
  voices.
- A readiness checklist is a better empty state than a blank list.
- Export can carry a manifest with provenance; the implementation/docs disagree on
  several details, so treat this as a design lesson, not a contract.

Licence: MIT covers the software; a reuse of code would require the MIT notice. No
code or assets were reused.

## 3. Professional audio UX

### Recording

- Request the microphone only after an explicit gesture; use an in-page explanation
  before the browser prompt.
- Check `navigator.permissions.query({name: "microphone"})` where available and handle
  denied state with recovery instructions, not a dead button.
- Handle `NotAllowedError`, `NotFoundError`, `NotReadableError`,
  `OverconstrainedError`, `AbortError` and `TypeError`. The permission prompt can
  remain unresolved if the user ignores it; provide a cancel/stalled state.
- `getUserMedia` is secure-context only. `127.0.0.1` is trustworthy; a LAN IP over
  HTTP is not.
- Level meter via `AnalyserNode`; visual updates around 30 Hz, ARIA value updates no
  more than a few times per second. Do not attach the meter to `aria-live`.
- Countdown default 3 s, cancellable, with a skip option. Stop on the up-event. Keep
  the previous take until retake is committed; offer undo.
- Hard floor around 1 s, recommended 3–15 s for a reference clip; cap around 30 s.
  These are product heuristics, not standards.

### Trim

- Waveform handles at least 24×24 CSS px; 44×44 on coarse pointers.
- Dragging must not be the only input: click-track positioning and numeric time
  fields are required alternatives (WCAG 2.2 SC 2.5.7).
- Handles are separate `role="slider"` controls with arrow keys and
  `aria-valuetext` such as “0:03.4 of 0:12.0”.
- Preview the selected region; do not require a full re-upload for a trim.

### Quality and consent

- Show pass/warn/fail as icon + text, never colour alone. Every warning names a
  remedy (“re-record closer to the microphone”).
- Use peak/clipping, silence ratio, loudness and sample-rate/channel checks. Do not
  present a similarity score that the engine cannot measure.
- Voice cloning needs an explicit, unchecked consent statement. The UI should state
  who owns the voice, purpose/scope, retention, deletion and prohibited uses. The
  exact copy is product/legal text, not a standard; have counsel review it.
- EU AI Act Art. 50 synthetic-audio marking/disclosure obligations apply from
  2026-08-02; GDPR Art. 13 requires information at collection.

### States, keyboard and accessibility

- Empty states communicate status, teach the object and offer the next action; never
  show “no records” while still loading.
- Use a skeleton for 1–10 s full-pane loads, progress for longer work, no flashing
  skeleton under 1 s.
- Errors belong inline next to the failing control. Toasts are for passive
  confirmation, not the only error path; use `role="status"` and a dismiss control.
- Command palette follows the APG combobox pattern; focus stays in the input and
  `aria-activedescendant` names the active option.
- WCAG 2.2 AA floor: 4.5:1 text, 3:1 UI/focus, visible focus, 24px minimum targets,
  keyboard alternatives, reduced motion, reflow at 320px.

Sources: W3C WCAG 2.2, Understanding 2.5.7/2.5.8/4.1.3, APG Combobox and Slider;
MDN getUserMedia, MediaRecorder, AnalyserNode, Permissions.query, `:focus-visible`,
`prefers-reduced-motion`; NN/g response times, skeleton screens and empty states;
EBU R 128; Google Cloud STT best practices; Partnership on AI synthetic media
practices; EU AI Act Art. 50; GDPR Art. 13.

## 4. Browser capture and upload

- `MediaRecorder` support is a format matrix, not one API:
  - Chromium: WebM/Opus by default; MP4/AAC only where the platform encoder exists.
  - Firefox: Ogg/Opus by default; WebM/Opus also accepted; MP4 is not recordable.
  - Safari 14.1–18.3: MP4/AAC. Safari 18.4 adds WebM/Ogg support, but runtime probing
    is still required.
- Probe `MediaRecorder.isTypeSupported()` for the full container/codec string. Use
  `recorder.mimeType` for the filename extension, never `Blob.type` alone.
- Chromium WebM is often “live” WebM without a duration; the server must decode it,
  and the UI should not depend on `<audio>` metadata before upload.
- `getUserMedia` audio constraints are best-effort on Firefox/Safari. Request mono
  and processing off, then display the actual `getSettings()`.
- Tauri/WebKitGTK Linux recording is **UNVERIFIED**. WebKitGTK MediaRecorder depends
  on GStreamer plugins and has reports of no supported type or empty recordings. The
  UI must feature-detect and fall back to file upload; never imply recording works.
- Web Audio `decodeAudioData` resamples to the context rate and needs a complete
  file. Server-side peaks via ffmpeg are authoritative; client peaks are a
  short-clip convenience only.
- Use XHR + `FormData` for upload progress and abort. `fetch` has no upload-progress
  event. Starlette does not cap file-part size, so the daemon must enforce its own
  request-body cap.
- Drag-and-drop needs a visible file input and keyboard activation; `accept` is only
  a hint and must be validated server-side.

Sources: MDN MediaRecorder, getUserMedia, MediaTrackConstraints, decodeAudioData,
XHR upload; Chromium MediaRecorder README; WebKit Safari 18.4 notes; WebKitGTK
multimedia docs; Tauri discussions/issues; W3C WCAG 2.2 and APG.

## 5. Untrusted audio ingestion security

`-protocol_whitelist file,pipe` is necessary but not sufficient. `file` is the
protocol a malicious `concat`, HLS, `image2`, `subfile` or MP4-`dref` input uses.
The load-bearing controls are:

1. Sniff the first 16 KiB (skip a bounded ID3v2 header) and map to a closed demuxer
   allowlist. Reject playlists, concat scripts, archives, executables, images and
   unknown signatures with `415`.
2. Force the demuxer with `-f` from that server-side table. Never accept a caller's
   `format` value.
3. `-protocol_whitelist file` (no network, no `concat:`, no `subfile:`); also
   `-format_whitelist` for a second layer.
4. Input-side `-t`, `-max_streams`, `-probesize`, `-analyzeduration`; output `-fs`;
   reject excessive channels/sample rate/dimensions.
5. `-nostdin`, `stdin=DEVNULL`, sanitized environment, `start_new_session=True`,
   timeout and TERM→KILL of the process group, bounded stderr.
6. Private 0700 staging dirs, `O_CREAT|O_EXCL|O_NOFOLLOW`, 0600 files, counted
   streaming writes, size/duration quotas and TTL cleanup.
7. Archives: resolve every member inside a fresh root, reject absolute paths, drive
   letters, backslashes, `..`, NUL, symlinks, duplicate case-folded names, encrypted
   entries; count decompressed bytes instead of trusting headers.

Current gaps found in this checkout:

- `audio_utils.py` passes `-i <path>` without a protocol/format whitelist or forced
  demuxer in `probe_media`, `extract_asr_wav`, `canonicalise_ref_audio`,
  `slice_clip`, `convert_audio` and `mux_video`.
- `/v1/audio/transcriptions` writes uploads without a byte cap and preserves an
  up-to-8-character client suffix, so a playlist name is accepted.
- Duration admission trusts the container metadata and has no input-side `-t`.
- `_run` has no `stdin=DEVNULL`, sanitized env, process-group kill, or timeout
  translation; timeout becomes a generic 500.
- `_escape_filter_path` escapes `\ : ' , [ ]` but not `;`.

The implementation must fix these at the shared layer, not only in the new route.
The acceptance matrix is in `tests/test_ingest_security.py`.

Sources: FFmpeg `ffmpeg(1)`, `ffmpeg-formats(1)`, `ffmpeg-protocols(1)`;
OWASP File Upload, SSRF, Path Traversal, DoS and Server-Side Media Rendering cheat
sheets; CWE-22/59/78/88/367/377/400/409/434/770/918; Python `tempfile`,
`subprocess`, `zipfile` documentation.

## 6. Current backend inventory

At `dbf7832`:

- REST: 31 documented paths plus the `/mcp` mount and `/health`. Only
  `/v1/audio/transcriptions` is multipart. `/v1/voices` accepts JSON and a confined
  local path only.
- Missing: history, captures, lexicon REST, cache REST, settings REST,
  `server_info` REST, auth check, diagnostics, SSE, and `/ui` serving.
- Stores: voices `data/voices/<id>/`, jobs `data/jobs/*.json`, cache
  `data/cache/`, artifacts `data/artifacts/` (or `<MCP_BASE_PATH>/artifacts`),
  pronunciations `data/pronunciations*`, bindings `data/bindings.json`.
- `JobStore.list()` exists but no route/CLI uses it. Install/download jobs only
  report 0.01 then 1.0.
- MCP `server_info` is the only server-info surface. No new MCP tools are needed;
  extract its payload builder for REST reuse.
- `docs/frontend/architecture.md` specifies the `/ui` serving contract but it is not
  implemented. The wheel contains only `src/agent_voice`; sdist excludes
  `frontend/`.
- `envfile.write_env` already provides atomic, comment-preserving, backed-up,
  0600 `.env` writes with secret masking; use it for settings.
- `GuardMiddleware` protects `/v1`, `/audio` and `/mcp`; `/health` is exempt.
  `/ui` is outside bearer prefixes but still gets Host/Origin checks.

## 7. Adopted / rejected

Adopted: voice-first selection; one navigation; persistent history and captures;
3-step clone flow; real recording/upload; trim + quality + transcript; multi-sample
storage with honest engine capability; job progress; previews; models licence
state; settings/diagnostics; original visual identity.

Rejected: copying any product layout or asset; a bundled translation model; a
UI-only backend; a token in a URL; path-based browser cloning; auto-demuxing
untrusted media; fabricated voice metadata or progress; multi-reference mixing on
engines that accept one reference.
