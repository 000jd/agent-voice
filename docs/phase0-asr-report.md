# Phase 0 — Phonon ASR CPU engine spike

Measured on the reference host on 2026-11-27, in a scratch `uv` venv, before any
`engines/phonon/` code. **Every number below comes from a run on this machine.**
Nothing is projected, extrapolated from a model card, or copied from a vendor
benchmark table; where a figure is the vendor's it is labelled as such.

## Decision

Use **`fermion-research==0.2.15`** with **CPU-only PyTorch**, driven through its
**Python API** (`import fermion; fermion.load_speech("phonon-2.5")`) rather than its
command line. It is Apache-2.0 code over CC-BY-4.0 weights, runs entirely on the
CPU, transcribes 45 s of speech in about 1 s on this host, and holds no VRAM at any
point.

Why the Python API and not the `phonon` binary:

- `phonon` is a wrapper that **pins `phonon-2`**, not `2.5`
  (`phonon --help`: "the fermion CLI with the model fixed to phonon-2"). Measured
  below: `phonon transcribe` downloaded `FermionResearch/Phonon-2` and never touched
  `2.5`. The alias we ship as the default is `phonon-2.5`, so the API is the only
  entry point that reaches it.
- A subprocess per transcription would add a process spawn and a model load per
  request; the API keeps the model resident in the worker, which is the whole point
  of the worker.

`whisper.cpp`, `faster-whisper` and `vosk` were not spiked. None of them is ruled
out — they are documented as *future* plugins in
[`engines/ADDING_AN_ENGINE.md`](../engines/ADDING_AN_ENGINE.md#6-adding-an-asr-engine),
which is the point of `kind: asr`: Phonon is the first ASR plugin, not the only one.

## Host

| | |
|---|---|
| CPU | AMD Ryzen 7 5700X 8-Core, 8 cores / **16** threads (`nproc` = 16) |
| GPU | NVIDIA GeForce RTX 4060 Ti 16 GB, driver **580.178.04** (deliberately unused) |
| OS / Python | Linux, Python **3.12.12** |
| `espeak-ng` | **1.50**, `/usr/bin/espeak-ng` (used to generate the test audio) |

## Exact dependency recipe

```bash
uv venv phonon-spike --python 3.12
uv pip install --python phonon-spike/bin/python \
  --index-url https://download.pytorch.org/whl/cpu torch
uv pip install --python phonon-spike/bin/python 'fermion-research==0.2.15'
```

```
Using CPython 3.12.12
Resolved 10 packages in 719ms
Prepared 9 packages in 2.73s
Installed 10 packages in 293ms
 + filelock==4.2.4
 + fsspec==2026.9.0
 + jinja2==3.1.6
 + markupsafe==3.0.3
 + mpmath==1.3.0
 + networkx==3.7
 + setuptools==78.1.0
 + sympy==1.14.0
 + torch==2.15.0+cpu
 + typing-extensions==4.16.0
```

The `fermion-research==0.2.15` install then added 31 packages, of which the ones
that matter are `transformers==5.24.1`, `huggingface-hub==2.5.0`,
`tokenizers==0.23.3`, `numpy==2.5.5`, `scipy==1.18.1`, `soundfile==0.14.0`,
`safetensors==0.8.0`, `zstandard==0.25.0`, `hf-xet==1.8.0`, `httpx2==2.16.2`,
`pydantic==2.14.4`, `typer==0.29.3`, `rich==15.0.0`.

**The critical trap is the same one Kokoro has.** `fermion-research` needs `torch`,
and installing it *alone* resolves the **CUDA** wheel from PyPI — every `nvidia-*`
package plus Triton, on the order of 2 GB, and `torch.cuda.is_available()` becomes
`True`. `torch` must be a **direct** dependency with an explicit CPU source, exactly
as `engines/kokoro/pyproject.toml` already does:

```toml
[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = { index = "pytorch-cpu" }
```

Installed this way, the CPU-only torch wheel pulled **no** `nvidia-*` package and
**no** Triton: the ten packages listed above are the whole closure.

## Zero-GPU proof

```text
torch.__version__       2.15.0+cpu
torch.cuda.is_available()   False
torch.version.cuda          None
```

`nvidia-smi --query-compute-apps=pid,used_memory --format=csv` was run at four
points in one process — before the import, after `load_speech`, while a 45 s
transcription was decoding, and after it returned:

```text
BEFORE:      'pid, used_gpu_memory [MiB]'      # header only, no rows
AFTER_LOAD:  'pid, used_gpu_memory [MiB]'
DURING:      'pid, used_gpu_memory [MiB]'
AFTER:       'pid, used_gpu_memory [MiB]'
```

Empty every time: **0 MB of VRAM**. The GPU's own `memory.used` moved between
449 MiB and 615 MiB across the session, which is the desktop baseline and other
processes, not this one — no compute PID ever appeared.

`fermion` also reports its own backend on load:

```
[fermion] decode backend: cpu · local model
```

The worker should still set `CUDA_VISIBLE_DEVICES=""` before importing torch, for
the same defensive reason Kokoro does: `fermion` selects its engine the same way
the CLI does (MLX on Apple silicon, the CPU engine elsewhere, `FERMION_DEVICE`),
and a stray `FERMION_DEVICE=cuda` in an operator's environment must not silently
make the "0 MB VRAM" claim false.

## Measured CPU performance

### Load time

| Run | Wall clock | Notes |
|---|---:|---|
| **First `load_speech` (cold)** | **50.13 s** | 0.16 GB download from the Hub, per-member SHA-256 verification, unpack to `~/.cache/fermion/speech/…`. Wall for the whole process incl. interpreter start and the transcribe: 50.92 s |
| Warm `load_speech` (cached) | **1.05 – 1.09 s** | four separate measurements: 1.089, 1.054, 1.083, 1.05 s |

The 50 s first call is real and is not a hang. `load_model` already has a 600 s
budget at the manager; **do not lower it**.

### Transcription throughput

Two `espeak-ng` clips, resampled once with
`ffmpeg -ac 1 -ar 16000 -sample_fmt s16`, transcribing with `phonon-2.5`,
`word_timestamps` on, warm model:

| Audio | Wall clock | Realtime multiple | Segments | Words | Peak RSS |
|---|---:|---:|---:|---:|---:|
| 16.71 s | 0.398 s | **42.0×** | 5 | 46 | — |
| 16.71 s | 0.424 s | **39.4×** | 5 | 46 | — |
| 16.71 s | 0.368 s | **45.4×** | 5 | 46 | — |
| 16.71 s | 0.349 s | **47.9×** | 5 | 46 | 943 MB¹ |
| 45.08 s | 1.021 s | **44.2×** | 13 | 125 | — |
| 45.08 s | 1.059 s | **42.6×** | 13 | 125 | — |
| 45.08 s | 1.080 s | **41.7×** | 13 | 125 | — |
| 45.08 s | 1.006 s | **44.8×** | 13 | 125 | 1.00 GB² |
| 45.08 s | 0.976 s | **46.2×** | 13 | 125 | — |

¹ peak RSS of a process that loaded the model and ran three 16.7 s clips plus one
hotword-biased clip: **965,356 KB = 943 MB**.
² peak RSS of a process that loaded the model and ran three 45 s clips:
**1,027,332 KB = 1.003 GB**.
First-ever run (download + unpack + transcribe): **1,369,352 KB = 1.31 GiB** — that
figure includes the unpack, so do not quote it as the steady-state footprint.

So the honest summary is **≈ 42–48× real time, ≈ 1.0 GB RSS warm**, from a single
process. The CPU was ~610 % busy during the 45 s runs (`Percent of CPU this job got:
612%`), i.e. the decoder already uses several threads on its own; there was no
attempt to tune `OMP_NUM_THREADS` or `torch.set_num_threads`.

These are **`espeak-ng` synthetic clips**, not human speech. Accuracy on them is
therefore not the model's accuracy — see the next section — but the *speed* is a
fair measure of decode cost, because the decoder does the same work either way.

`r.audio_seconds` and `r.decode_seconds` are both exposed by the result, so the
worker can report `realtime_multiple` from the backend's own number rather than
wrapping a `perf_counter` around it. `r.audio_seconds` matched the ffprobe duration
exactly (`45.078`).

## What the backend actually returns

This is the part the worker must be built around, so it is recorded rather than
summarised. `speech.transcribe_detailed(path, hotwords=[...])` returned an object
with these attributes:

```text
['audio_seconds', 'decode_seconds', 'generation_tokens', 'segments', 'text',
 'timed', 'tokens', 'triple', 'truncated', 'windows', 'words']
```

- `segments` is a **list of dicts**, each `{'id': int, 'start': float, 'end': float,
  'text': str}`. On a 45 s clip it returned **13** segments, and they *did* respect
  sentence punctuation (`'A circle means the set of everything inside it.'` is one
  segment, 0.00–3.03 s). Segment grouping is therefore a bonus, not a requirement:
  the worker still owns the three documented rules (punctuation, a ≥ 0.8 s pause, a
  7 s cap) because `segments` is not part of the documented surface and an older or
  different profile may omit it.
- `words` is a list of dicts, each `{'text': str, 'start': float, 'end': float}` —
  note the key is **`text`**, not `word`, so the worker maps it. Word timings were
  present because `transcribe_detailed` asks for them; the plain `transcribe()`
  returns only `(text, decode_s, audio_s)`.
- **There is no `language` attribute at all.** `[a for a in dir(r) if 'lang' in
  a.lower()]` returned `[]`. Phonon-2.5 auto-detects and does not report what it
  picked, so the worker must report `"auto"` rather than invent a language. This
  confirms the plan's note; it is not an assumption.
- **There is no confidence or probability field** anywhere on the result, so nothing
  of the sort can be exposed. Do not invent one.
- `truncated` is a real boolean on the result (`False` on every clip here) and should
  be passed through rather than recomputed.
- `hotwords=[...]` is a real keyword (documented cap: 25 terms on Phonon-2). Passing
  `hotwords=["Phonon", "CUDA"]` did not change this clip's text, which is the
  correct behaviour for terms that are not in the audio.
- `fermion.load_speech(model, *, quiet=True)` takes an alias or a repo id;
  `"phonon-2.5"` resolved to `FermionResearch/Phonon-2.5` and its
  `five-value-25` profile automatically.

### Accuracy observed on synthetic speech (honest, not a benchmark)

`espeak-ng` output is a hard input — flat prosody, no natural coarticulation — and
the transcripts showed it:

| Spoken | Transcribed (`phonon-2.5`) |
|---|---|
| "The derivative of x squared is two x" | "The derivative of x squared is **to** x" |
| "2x" (written form re-spoken) | "**max**" *(phonon-2, via the `phonon` CLI)* |
| "with no graphics memory" | "with **narographics** memory" *(phonon-2.5)* / "**neurographics** memory" *(phonon-2)* |

Everything else in both clips came back verbatim, including the multi-clause
sentences, the comma placement and the numerals. Treat this as "the plumbing works
and the model is a real recogniser", **not** as a WER measurement: the vendor's own
figure for `phonon-2.5` is 5.06 % average word error across the seven English Open
ASR Leaderboard sets, and 11.71 % across 25 languages on FLEURS. Those are the
model card's numbers, reproduced here only as context.

## Model and cache paths

The download is a **packed `.fsq` archive**, not plain `.safetensors` — which is
why `engine_registry.fermion_model_downloaded()` looks for the unpacked directory
*and* `*.fsq` / `*.tar.zst` in the HF cache, rather than at weight files.

What appeared on this machine:

```text
~/.cache/huggingface/hub/models--FermionResearch--Phonon-2.5/
├── refs/main
├── blobs/<sha256>              (phonon-2.5.fsq → 156,181,652 bytes = 156.2 MB)
├── blobs/<sha256>              (config.json, README.md, NOTICE)
└── snapshots/<rev>/
    ├── phonon-2.5.fsq  -> ../../blobs/<sha256>
    ├── config.json     -> ../../blobs/<sha256>
    ├── README.md       -> ../../blobs/<sha256>
    └── NOTICE          -> ../../blobs/<sha256>

~/.cache/fermion/speech/FermionResearch__Phonon-2.5/model_phonon25_f25x1_48k_int6/
├── config.json               277,495 bytes
├── model.fermion         177,593,644 bytes
└── packed_manifest.json           503 bytes
```

`~/.cache/fermion/speech/FermionResearch__Phonon-2.5` is **170 MB** on disk after
unpacking (the `.fsq` is 156.2 MB; both stay, so a Phonon install costs ~326 MB
until the archive is cleared). `FERMION_CACHE_DIR` replaces `~/.cache/fermion`
entirely, which is why the registry checks both.

`phonon transcribe` — the CLI, pinned to `phonon-2` — additionally downloaded
`~/.cache/fermion/speech/FermionResearch__Phonon-2/model_phonon2_c4c_int6`
(170 MB) and `models--FermionResearch--Phonon-2` in the HF cache. That is how the
`phonon-2` row in the table above was measured. Nothing else was written outside
those two cache roots.

## Licences (verified from the artefact, not assumed)

- **HF front matter:** `license: cc-by-4.0` on all three model cards
  (`Phonon-2.5`, `Phonon-2.5-English`, `Phonon-2`).
- **`NOTICE` inside `FermionResearch/Phonon-2.5`** (2664 bytes, shipped in the
  snapshot) states it plainly: the weights derive from NVIDIA's Parakeet TDT 0.6B
  v3, "these weights are therefore distributed under CC-BY-4.0", and "the Fermion
  code in this repository remains under the Apache License, Version 2.0".
- **The `fermion-research` Python package is Apache-2.0** (PyPI metadata).

So the split is **CC-BY-4.0 weights / Apache-2.0 code**, and CC-BY requires
attribution on redistribution — the `NOTICE` is the notice to keep. This repo only
downloads the weights and does not redistribute them, but an operator who mirrors
them does.

**Sizes** (from the model cards, all three confirmed present as downloads):

| Model | Download | What it is |
|---|---:|---|
| `phonon-2.5` | **156.2 MB** | 25 languages, 5.06 % avg WER on the 7 English ASR sets, 11.71 % on FLEURS-25 |
| `phonon-2.5-en` | **157.6 MB** | English only, 4.82 % avg WER |
| `phonon-2` | **164 MB** | English only, 5.21 % avg WER; the alias the `phonon` CLI pins |

All three are 0.6B-parameter FastConformer/TDT models compressed to int6.

## Caveats to carry into the worker

- **Auto-detect only.** No per-request language control on 2.5, and no reported
  language. Report `"auto"`; never guess.
- **No confidence field.** Nothing to surface, so surface nothing.
- **Segments are a bonus.** Use the backend's `segments` when present, otherwise
  group `words` locally with the three documented rules.
- **The first call is 50 s.** Not a timeout; keep the 600 s `load_model` budget.
- **~1 GB RSS per worker**, and it is a real allocation, not a reservation: an
  operator sizing a box should plan for ~1 GB per ASR worker plus the model's own
  page cache.
- **`espeak-ng` is not a WER benchmark.** Human speech is the only fair input; the
  throughput numbers here hold either way.

Sources: [FermionResearch/Phonon-2.5](https://huggingface.co/FermionResearch/Phonon-2.5),
[FermionResearch/Phonon-2.5-English](https://huggingface.co/FermionResearch/Phonon-2.5-English),
[FermionResearch/Phonon-2](https://huggingface.co/FermionResearch/Phonon-2),
[fermion-research on PyPI](https://pypi.org/project/fermion-research/),
[PyTorch CPU wheel index](https://download.pytorch.org/whl/cpu),
[Fermion docs](https://www.fermionresearch.com/docs/speech/),
[nvidia/parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3),
[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/).
