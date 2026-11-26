# Phase 0 — Kokoro-82M CPU engine spike

Measured on the reference host on 2026-11-26, before any `engines/kokoro/` code.

## Decision

Use **`kokoro==0.9.4`** with **CPU-only PyTorch** and the HF cache
(`hexgrad/Kokoro-82M`). It is Apache-2.0, runs entirely on the CPU, emits native
24,000 Hz mono, and uses `hf_hub_download`, so `agent-voice models download` and
`hf_model_downloaded()` work without a custom downloader.

`kokoro-onnx==0.6.1` is lighter (MIT code, Apache-2.0 model, no torch) but its
model files are GitHub release assets (`kokoro-v1.0.onnx`, `voices-v1.0.bin`), not
HF. It would require a separate downloader and would not integrate with the
existing HF-cache/model-discovery contract. It is documented as the alternative,
not shipped.

## Host

| | |
|---|---|
| CPU | AMD Ryzen 7 5700X, 8 cores / 16 threads |
| GPU | NVIDIA RTX 4060 Ti 16 GB (deliberately unused) |
| OS / Python | Linux, Python 3.12.12 |
| `espeak-ng` | present at `/usr/bin/espeak-ng` |

## Exact dependency recipe

The critical trap: adding `torch` only as a transitive dependency of `kokoro`
resolves the **CUDA** wheel from PyPI and pulls every `nvidia-*` package plus
Triton. `torch` must be a **direct** dependency with an explicit CPU source:

```toml
[project]
requires-python = ">=3.12,<3.13"
dependencies = [
  "kokoro==0.9.4",
  "torch==2.15.0",
  "en-core-web-sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl",
  "soundfile>=0.12",
  "numpy>=1.26",
  "huggingface-hub>=0.26",
]

[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = { index = "pytorch-cpu" }
```

- `en-core-web-sm==3.8.0` is required by `misaki[en]` for English G2P. `misaki`
  tries to download it at first use; that installer can select the **active**
  interpreter (in the spike it wrote into the repo `.venv`, not the spike venv)
  and the pipeline then fails with `OSError: [E050] Can't find model
  'en_core_web_sm'`. Pin the model as a direct URL dependency so `uv sync
  --project engines/kokoro` installs it into the engine venv.
- The successful sync removed 20 packages and installed exactly one:
  `torch==2.15.0+cpu`; no `nvidia-*`, no `triton`.

## Zero-GPU proof

`torch.__version__ == "2.15.0+cpu"`, `torch.cuda.is_available() == False`,
`torch.version.cuda is None`; `nvidia-smi --query-compute-apps` is empty before,
during and after three renders. `KPipeline` auto-selects CUDA when `device=None`,
so the worker must pass `device="cpu"` and set `CUDA_VISIBLE_DEVICES=""` before
importing torch.

## Measured CPU performance

Pipeline load (first call, weights already cached): **2.05 s**; process peak RSS
**1.37 GB**. Three utterances, `voice='af_heart'`, `speed=1`:

| Text | Audio | Wall clock | Realtime multiple | Peak RSS |
|---|---:|---:|---:|---:|
| A circle means the set of everything inside it. | 3.350 s | 0.983 s | **3.41×** | 1.26 GB |
| A square means a fixed boundary that never moves. | 3.275 s | 0.626 s | **5.23×** | 1.26 GB |
| The quick brown fox jumps over the lazy dog, then explains the derivative of x squared. | 5.900 s | 1.253 s | **4.71×** | 1.37 GB |

Audio sample counts are exactly `duration × 24_000`, written as 24 kHz WAV.
These are single-threaded-default `KPipeline` numbers; the worker can set
`torch.set_num_threads` to the available cores, but do not expect linear scaling
on an 82M model.

## Model / download

- HF repo `hexgrad/Kokoro-82M`: **363 MB**; `kokoro-v1_0.pth` **327 MB**;
  `config.json`; `voices/*.pt` (54 voices, 8 languages).
- `KPipeline` calls `hf_hub_download` for `config.json`, `kokoro-v1_0.pth` and
  `voices/<voice>.pt`. Pre-populating the HF cache with
  `agent-voice models download kokoro kokoro-82m` is sufficient. `hf-xet` is
  installed transitively but was disabled for the spike
  (`HF_HUB_DISABLE_XET=1`) to avoid the finalization hang seen on the Qwen
  downloads; the engine should keep the same env override during installs.
- English voices advertised: `af_heart`, `af_bella`, `af_sarah`, `am_adam`,
  `am_michael`, `bf_emma`, `bm_george`, `bm_lewis`. The manifest should start
  with `languages: [Auto, English]`; other Kokoro languages need extra `misaki`
  extras/verification.

## espeak-ng

`misaki`'s English G2P uses spaCy; `espeak-ng` is the OOD fallback and the
non-English backend. The `espeakng-loader` wheel emitted

```
Bad voice attribute: option
Error processing file '/home/runner/work/espeakng-loader/.../espeak-ng-data/phontab': ...
```

but the English G2P and synthesis still succeeded. The host's
`/usr/bin/espeak-ng` is present; `doctor` should report it and WARN (not FAIL)
if missing, because common English works without the fallback while non-English
and OOD words do not.

Sources: [kokoro 0.9.4 on PyPI](https://pypi.org/project/kokoro/),
[hexgrad/Kokoro-82M model card](https://huggingface.co/hexgrad/Kokoro-82M),
[hexgrad/kokoro pipeline.py](https://github.com/hexgrad/kokoro),
[Kokoro VOICES.md](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md),
[kokoro-onnx](https://pypi.org/project/kokoro-onnx/).
