# Phase 0 & hardware verification report

All numbers below were **measured on this machine**, not estimated.

## Host

| Item | Value |
|---|---|
| OS | Pop!_OS 22.04 LTS (Ubuntu 22.04, jammy) |
| Kernel driver | NVIDIA 580.178.04 |
| GPU | RTX 4060 Ti 16 GB, PCI `10de:2803`, compute capability **(8, 9)** |
| GPU total VRAM | 16 380 MiB |
| Desktop VRAM baseline (idle) | **631–697 MiB** |
| `ffmpeg` / `ffprobe` | present at `/usr/bin` |
| `uv` | 0.9.26 at `~/.local/bin/uv` |
| Python | 3.12.12 (uv-managed); system Python is 3.10.12 |

## Corrections to the original brief

Four assumptions in the build prompt did not survive contact with the machine.

### 1. The 0.6B model was *not* already downloaded

`~/.cache/huggingface/hub/models--Qwen--Qwen3-TTS-12Hz-0.6B-Base/` contained only
`refs/main` — a 12 KB pointer, no `snapshots/`, no weights. The entire HF blob
store held 95 MB, all belonging to Depth-Anything.

A naive "does the directory exist" check would have reported the model as
present. `engine_registry.hf_model_downloaded()` therefore walks the snapshot
tree looking for real `.safetensors`/`.bin`/`.pt`/`.gguf` files **with nonzero
size**, and is covered by a regression test.

Both models were downloaded during this build:

```
Qwen/Qwen3-TTS-12Hz-0.6B-Base   2.4 GB   (~1.83 GB model.safetensors)
Qwen/Qwen3-TTS-12Hz-1.7B-Base   ~3.5 GB
```

### 2. `qwen-tts` and `faster-qwen3-tts` cannot share a virtualenv

| Package | Pins |
|---|---|
| `qwen-tts==0.1.1` | `transformers==4.57.3` |
| `faster-qwen3-tts==0.5.4` | `transformers>=5.15.1,<6` + `qwen-tts-hf` |

`qwen-tts-hf` is a compatibility build that provides the **same `qwen_tts`
import name** with opposite pins. Installing both distributions in one
environment is explicitly unsupported.

**Consequence:** two engine plugins, two venvs, two `backend_name`s:

| engine_id | dist | transformers | torch | streaming |
|---|---|---|---|---|
| `qwen3_tts` | `qwen-tts 0.1.1` | 4.57.3 | 2.11.0+cu128 | emulated |
| `qwen3_tts_fast` | `faster-qwen3-tts 0.5.4` + `qwen-tts-hf 0.1.1.post1` | **5.15.1** | 2.11.0+cu128 | **native** |

### 3. `transformers` must be pinned exactly to 5.15.1 for the fast backend

Resolving `transformers>=5.15.1,<6` installed **5.24.1**, and the model failed to
load at all:

```
File ".../qwen_tts/core/tokenizer_12hz/modeling_qwen3_tts_tokenizer_v2.py", line 900
    self.upsample = None
AttributeError: property 'upsample' of 'Qwen3TTSTokenizerV2Encoder' object has no setter
```

The tokenizer assigns `self.upsample`, but newer transformers exposes `upsample`
as a read-only property. Pinning `transformers==5.15.1` fixes it; CUDA graphs
then captured cleanly:

```
Warming up predictor (3 runs)...   Capturing CUDA graph for predictor... CUDA graph captured!
Warming up talker graph (3 runs)... Capturing CUDA graph for talker decode... Talker CUDA graph captured!
```

### 4. `flash-attn` is not used

The official docs only *recommend* it. Building it on a 32 GB box is a known
trap. Every engine uses `attn_implementation="sdpa"` with `bfloat16`.

## FastMCP 4.2.6 API findings

Verified empirically with a scratch venv and 17 tests.

| Finding | Detail |
|---|---|
| Mount pattern | `mcp_app = mcp.http_app(path="/")`, `app.mount("/mcp", mcp_app)`, lifespan **must** be composed |
| Failure without lifespan | `FastMCP's StreamableHTTPSessionManager task group was not initialized…` (three lines; `/health` still 200, only `/mcp` returns 500) |
| Composition helper | `from fastmcp.utilities.lifespan import combine_lifespans` — no other path exists |
| `get_http_headers()` | excludes `authorization` and `cookie` by default; `include={"authorization"}` re-adds them. Returns `{}` (never raises) outside HTTP |
| Bearer middleware | Applied on the **parent** app, it covers the mounted `/mcp` sub-app |
| Client URL | Trailing slash required: `/mcp` → 307, `/mcp/` → 200 |
| `FastMCP.as_proxy` | **Removed in v4.** Use `ProxyProvider(client_factory)` via `proxy.add_provider(...)` |
| Client factory protocol | Under-declares its args; always add `**kwargs` (fastmcp passes `follow_redirects`) |
| `run_asgi_lifespan` | **Broken with `combine_lifespans`**: builds a lifespan scope without `"state"`, which Starlette rejects. Test harness supplies `"state": {}` itself |

## Measured performance (official backend, 0.6B, RTX 4060 Ti)

| Metric | Value |
|---|---|
| Daemon boot, GPU engine configured | < 1 s, **0 workers** |
| VRAM at boot | **631 MiB** (= desktop baseline, i.e. nothing held) |
| Cold start (spawn + load + prompt extract + 9.5 s speech) | **21.84 s** |
| Warm request (model resident, 9.0 s speech) | **10.49 s** |
| Cache hit (identical text) | **0.01 s** |
| VRAM with 0.6B loaded | **3 505–3 526 MiB** (~2.9 GB over baseline) |
| VRAM after `unload(force=true)` | **639 MiB** — back to baseline |
| Barge-in cancel latency | **< 200 ms** (asserted by the GPU test) |

## Test results

```
Non-GPU suite : 196 passed, 13 deselected (mock engine only, no torch)
GPU suite     :  13 passed  (AGENT_VOICE_RUN_GPU_TESTS=1, -m gpu -o addopts="")
Lint          : all checks passed (ruff)
```

GPU suite coverage: both engines × both model sizes clone and synthesize, MP3
encoding, native PCM streaming, barge-in cancel < 200 ms, VRAM returns to
baseline after unload, and cache hits avoid respawning the worker.

## Architectural influences (ideas only, no code)

`debpalash/VoiceStudio` is **AGPL-3.0**, and this project is MIT. No files were
copied. For the record, VoiceStudio's layout differs from this one: it uses
`backend/engines/<id>/main.py` modules plus a central `backend/config/models.yaml`,
and has **no per-engine `engine.yaml`**. The per-engine manifest here is this
project's own design, chosen because it makes the backend identity (and therefore
the synthesis cache key) knowable before a worker is ever spawned.

Ideas borrowed: isolated per-engine dependency universes, a disk-first job store,
and strict path confinement for agent-supplied paths.
