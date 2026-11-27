# Phase 0 — device variants (CPU / CUDA / MPS / ROCm)

Measured on the reference host on 2026-11-27, in a throwaway copy of
`engines/qwen3_tts` under `/tmp/variant-test`. **Every command and output below
was run in this session**; nothing is projected from a vendor page. Where a claim
comes from another report instead of a run here, it says so.

Scope of this document: prove or disprove the v0.6.0 device-variant mechanism
(`engines/<id>/variants/<device>/` + `UV_PROJECT_ENVIRONMENT` + `uv sync --locked`),
and establish, with evidence, which `(engine, device)` pairs may be called
`verified`.

**Safety.** No source file, no `engines/*/.venv` and no committed `uv.lock` was
touched. `sha256sum engines/qwen3_tts/uv.lock` was taken before and after every
experiment and never changed. `git status --short engines/` is empty at the end of
the session. The only repo file written is this one.

## Decision

1. **The variant mechanism works, exactly as specified.** A per-device project
   root under `variants/<device>/` with its own `pyproject.toml` + `uv.lock`,
   synced with `UV_PROJECT_ENVIRONMENT=<engine>/.venv uv sync --project
   variants/<device> --locked`, creates and populates the **canonical**
   `engines/<id>/.venv` and leaves the canonical `uv.lock` byte-identical. **No
   symlink fallback is needed.** Verified on uv 0.9.26.
2. **`--locked` is not optional.** Without it, `uv sync` silently rewrites the
   variant lock (measured: sha `5e151d2…` → `38ac480…`, plus one new package).
   With it, drift is refused with exit code 1 and the lock is untouched.
3. **An index flag cannot select a device.** `UV_INDEX`, `UV_DEFAULT_INDEX`,
   `--index`, `--default-index` and `--index-url` are all *ignored* by
   `[tool.uv.sources]`. Worse, `--index`/`UV_INDEX` on the real CUDA project
   silently changed the locked torch from `2.11.0+cu128` to `2.9.1+cu128` while
   keeping the `whl/cu128` source, producing a **torch/torchaudio version
   mismatch**. The device must be a separate lock root, as the plan assumed.
4. **ROCm is lockable — with two conditions the plan did not anticipate.** The
   index must be **non-`explicit`** (torch's own `rocm[device-all]` and
   `triton-rocm` dependencies live on the same PyTorch index) and
   `[tool.uv] environments` must be narrowed to `linux`/`x86_64` (otherwise uv
   backtracks to `torch 2.0.1`, whose only wheels at that index are aarch64).
   With both, `uv lock` produces `torch 2.15.0+rocm7.14`. It cannot be **run**
   here (no AMD GPU), so the pair stays `experimental`.
5. **MPS is lockable from Linux.** `uv lock` resolves `torch 2.15.0` with
   `macosx_14_0_arm64` cp311/cp312 wheels, both unrestricted (114 packages) and
   narrowed to `darwin`/`arm64` (94 packages). Runtime on Apple silicon is
   unverified here, so the pair stays `experimental`.
6. **`(qwen3_tts, cpu)` produces real audio.** Two independent runs: the model
   directly (`torch.float32`, `device_map="cpu"`) and the **shipped worker** over
   real IPC in the temp copy. Both wrote valid 24 kHz mono WAVs with non-zero
   loudness. The committed worker cannot do it unmodified — it hardcodes
   `device_map="cuda:0"` / `dtype=torch.bfloat16` — which is exactly the Wave 2
   change; with that one change applied in the temp copy it runs end to end.

## Host

| | |
|---|---|
| OS | Pop!_OS 22.04 LTS, kernel `7.2.15-76070215-generic`, x86-64, glibc **2.35** |
| CPU | AMD Ryzen 7 5700X 8-Core, 8 cores / **16** threads, 31 GiB RAM (17 GiB available) |
| GPU | NVIDIA GeForce RTX 4060 Ti **16 380 MiB**, driver **580.178.04**, 695 MiB in use at idle |
| Python | **3.12.12** (uv-managed), system Python 3.10.12 |
| `uv` | **0.9.26** |
| Tools | `ffmpeg`/`ffprobe` `/usr/bin`, `espeak-ng` `/usr/bin` |
| ROCm | **absent** — no `rocm-smi`, no `rocminfo`, no `/dev/kfd`; only `/dev/dri/renderD128` |
| HEAD at time of writing | `fd20ee3` (Wave 0.5 commits landed on top of the plan's base `0246687`) |

The shell had an activated `VIRTUAL_ENV` (`agent-voice/.venv`); `uv` warns about
it and ignores it, because `UV_PROJECT_ENVIRONMENT` wins:

```
warning: `VIRTUAL_ENV=/home/joydip/Documents/agent-voice/.venv` does not match the
project environment path `engine/.venv` and will be ignored; use `--active` to
target the active environment instead
```

`engines install` must therefore set `UV_PROJECT_ENVIRONMENT` explicitly and must
not assume `VIRTUAL_ENV` is unset (later runs below use `env -u VIRTUAL_ENV` so the
warning does not appear; the outcome is identical either way).

## Why a device cannot be a `uv sync` flag

The committed locks already disagree about torch, so a device is a different
dependency set by construction:

| Lock | torch | torchaudio | index |
|---|---|---|---|
| `engines/qwen3_tts/uv.lock` | `2.11.0+cu128` | `2.11.0+cu128` | `https://download.pytorch.org/whl/cu128` |
| `engines/kokoro/uv.lock` | `2.15.0+cpu` | — | `https://download.pytorch.org/whl/cpu` |

The canonical lock's own source line, `engines/qwen3_tts/uv.lock:1749`:

```toml
source = { registry = "https://download.pytorch.org/whl/cu128" }
```

Baseline recorded before any experiment:

```
$ sha256sum engines/qwen3_tts/uv.lock
92d04ab54ffc0d7501f9b61454f9805fba432f26cb89c7d0308e89f5899d6698  engines/qwen3_tts/uv.lock
$ stat -c '%s bytes  mtime=%y' engines/qwen3_tts/uv.lock
253106 bytes  mtime=2026-11-26 07:28:42.828118124 +0530
```

## Experiment 1 — the variant mechanism, proven

### 1.0 Throwaway copy

```bash
mkdir -p /tmp/variant-test
rsync -a --exclude '.venv/' --exclude '__pycache__/' \
      engines/qwen3_tts/ /tmp/variant-test/engine/
```

```
engine.yaml  pyproject.toml  uv.lock  worker.py
```

`.venv` was excluded on purpose so the sync has to **create** the venv; that is
what makes the "does it land at the canonical path" question meaningful.

```
$ sha256sum /tmp/variant-test/engine/uv.lock
92d04ab54ffc0d7501f9b61454f9805fba432f26cb89c7d0308e89f5899d6698  /tmp/variant-test/engine/uv.lock
```

### 1.1 The CPU variant project

`/tmp/variant-test/engine/variants/cpu/pyproject.toml` — identical project
metadata and dependency list to `../../pyproject.toml`; only the wheel index
changes:

```toml
[project]
name = "agent-voice-engine-qwen3-tts"
version = "0.1.0"
description = "Official Qwen3-TTS backend for agent-voice (CPU variant)"
requires-python = ">=3.11,<3.13"

# Same dependency set as ../../pyproject.toml; only the torch source changes.
dependencies = [
    "qwen-tts==0.1.1",
    "torch",
    "torchaudio",
    "soundfile",
    "numpy",
]

[tool.uv]
[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = [{ index = "pytorch-cpu" }]
torchaudio = [{ index = "pytorch-cpu" }]

[tool.agent-voice]
device = "cpu"
```

### 1.2 Lock it

```
$ time uv lock --project /tmp/variant-test/engine/variants/cpu
Using CPython 3.12.12
Resolved 97 packages in 2.82s
0.10user 0.18system 0:02.87elapsed 10%CPU
```

`uv.lock` appears **next to the variant pyproject**, at
`/tmp/variant-test/engine/variants/cpu/uv.lock`, sha256
`5e151d2fb8255739940ceeea7cc1b9a3454a5b0e066c975fa8523e5589302667`. The canonical
lock is unchanged (`sha256sum uv.lock` → `92d04ab5…`).

What the variant lock says about torch (`variants/cpu/uv.lock:1547-1625`):

```
name = "torch"
version = "2.15.0"
source = { registry = "https://download.pytorch.org/whl/cpu" }      # sys_platform == 'darwin'
...
name = "torch"
version = "2.15.0+cpu"
source = { registry = "https://download.pytorch.org/whl/cpu" }      # everything else
```

`grep -c 'name = "nvidia' uv.lock` → **0**. `grep -c 'name = "triton"'` → **0**.
The CPU closure contains no NVIDIA package and no Triton, which is the whole
point of pinning the index.

One thing worth recording: the CPU index lags on torchaudio, so the pair is
**torch 2.15.0+cpu / torchaudio 2.11.1+cpu** — not a matching version pair. This
is not a lock bug: `torchaudio-2.11.1+cpu.dist-info/METADATA` declares only

```
Requires-Dist: torch>=2.11
```

and one torchaudio op was exercised against torch 2.15.0 successfully (below).
It is still a deliberate, visible difference from the CUDA lock
(`2.11.0+cu128`/`2.11.0+cu128`), and `docs/engines.md` should not claim the two
variants hold the same torch.

### 1.3 Sync into the canonical environment

```
$ UV_PROJECT_ENVIRONMENT=/tmp/variant-test/engine/.venv \
    uv sync --project /tmp/variant-test/engine/variants/cpu --locked
Using CPython 3.12.12
Creating virtual environment at: engine/.venv
Resolved 97 packages in 0.72ms
Prepared 1 package in 395ms
Installed 88 packages in 363ms
 + accelerate==1.12.0
 ...
 + qwen-tts==0.1.1
 ...
 + torch==2.15.0+cpu
 + torchaudio==2.11.1+cpu
 + transformers==4.57.3
 ...
 + uvicorn==0.54.0
0.08user 0.59system 0:00.92elapsed 73%CPU
```

(88 packages; list abridged. `Prepared 1 package` because every other wheel was
already in the shared uv cache.)

### 1.4 Confirmations

```
$ ls -d /tmp/variant-test/engine/.venv && readlink -f /tmp/variant-test/engine/.venv
/tmp/variant-test/engine/.venv
/tmp/variant-test/engine/.venv

$ ls -a /tmp/variant-test/engine/variants/cpu/
.  ..  pyproject.toml  uv.lock          # no .venv here
```

```
$ /tmp/variant-test/engine/.venv/bin/python -c "import torch, torchaudio, sys; ..."
python            3.12.12
torch.__version__ 2.15.0+cpu          <-- ends in +cpu
torch.version.cuda None               <-- is None
torch.version.hip None
torch.cuda.is_available() False
torchaudio.__version__ 2.11.1+cpu
mps available     False
```

```
$ du -sh /tmp/variant-test/engine/.venv                  -> 1.6G
$ du -sh engines/qwen3_tts/.venv   (canonical, cu128)    -> 7.3G
```

### 1.5 The canonical lock is byte-identical

```
$ sha256sum engines/qwen3_tts/uv.lock
92d04ab54ffc0d7501f9b61454f9805fba432f26cb89c7d0308e89f5899d6698  engines/qwen3_tts/uv.lock
$ stat -c '%s bytes  mtime=%y' engines/qwen3_tts/uv.lock
253106 bytes  mtime=2026-11-26 07:28:42.828118124 +0530
$ git status --short engines/
                                        # empty
```

Same sha256 **and** same mtime, so the file was not rewritten with identical
content — it was never opened for writing.

## Experiment 2 — `--locked` is what makes it safe

### 2.1 Drift is refused

`variants/cpu/pyproject.toml` was edited to add `"six"` to `dependencies` **without
re-locking**, then synced again with the same flags:

```
$ uv sync --project /tmp/variant-test/engine/variants/cpu --locked
Resolved 98 packages in 403ms
The lockfile at `uv.lock` needs to be updated, but `--locked` was provided. To update the lockfile, run `uv lock`.
$ echo $?
1
```

The variant lock's sha256 was `5e151d2fb8255739940ceeea7cc1b9a3454a5b0e066c975fa8523e5589302667`
before and after that failed run — **unchanged**. Nothing was installed.

### 2.2 Control: without `--locked` the lock is silently rewritten

Same drifted project, same venv, `--locked` dropped:

```
$ uv sync --project /tmp/variant-test/engine/variants/cpu
Resolved 98 packages in 51ms
Installed 1 package in 2ms
 + six==1.17.0
```

```
before: 5e151d2fb8255739940ceeea7cc1b9a3454a5b0e066c975fa8523e5589302667
after:  38ac480ad248ca5849b3fde054dc4641fffc7c0cd27bf8c80d8925834c0e10b1
        and `grep -A2 'name = "six"' uv.lock` now shows { name = "six" } in the
        project's dependency list
```

This is the hazard the plan names: a plain `uv sync` against a variant whose
pyproject has drifted **rewrites the committed lock**. `--locked` is the guard,
and it is the reason the installer must use it, not an optimisation.

### 2.3 Restore, and proof that sync is exact

pyproject and lock were restored from the backups taken before the edit; the sha
returned to `5e151d2…`. Re-syncing with `--locked` then *removed* the package the
drifted sync had added:

```
$ uv sync --project .../variants/cpu --locked
Resolved 97 packages in 0.60ms
Uninstalled 1 package in 0.26ms
 - six==1.17.0
$ .venv/bin/python -c "import six"
ModuleNotFoundError: No module named 'six'
```

`uv sync` prunes as well as installs, so the environment after a sync is exactly
the variant's lock — which is what makes in-place device switching (Experiment 8)
sound.

## Experiment 3 — index flags cannot select a device

Two probes. The first isolates the question with a project that has nothing but
`torch` and a CPU pin, so `UV_DEFAULT_INDEX` can still resolve (PyPI is only
needed for nothing at all). Every run started from a directory containing **only**
`pyproject.toml`, so the lock had to be produced from scratch.

```toml
[project]
name = "probe"
version = "0.0.0"
requires-python = ">=3.12,<3.13"
dependencies = ["torch"]

[tool.uv]
[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = [{ index = "pytorch-cpu" }]
```

| Run | Command | Resulting `torch` |
|---|---|---|
| p0 | `uv lock` | `2.15.0+cpu` ← `https://download.pytorch.org/whl/cpu` |
| p1 | `UV_DEFAULT_INDEX=https://download.pytorch.org/whl/cu128 uv lock` | `2.15.0+cpu` ← `whl/cpu` |
| p2 | `UV_INDEX=https://download.pytorch.org/whl/cu128 uv lock` | `2.15.0+cpu` ← `whl/cpu` |
| p3 | `UV_INDEX_STRATEGY=unsafe-best-match UV_INDEX=…/cu128 uv lock` | `2.15.0+cpu` ← `whl/cpu` |
| p4 | `uv lock --index https://download.pytorch.org/whl/cu128` | `2.15.0+cpu` ← `whl/cpu` |
| p5 | `uv lock --default-index https://download.pytorch.org/whl/cu128` | `2.15.0+cpu` ← `whl/cpu` |
| p6 | `uv lock --index-url https://download.pytorch.org/whl/cu128` (deprecated alias) | `2.15.0+cpu` ← `whl/cpu` |

Seven for seven: **`[tool.uv.sources]` wins over every index flag and every
`UV_*_INDEX` variable**, including `unsafe-best-match`. The flags cannot move
torch to another wheel index.

The second probe is the real `engines/qwen3_tts/pyproject.toml` (pinned to
`pytorch-cu128`), each run in a fresh copy with no lock:

| Run | Command | Result |
|---|---|---|
| c0 | `uv lock` | torch `2.11.0+cu128`, torchaudio `2.11.0+cu128`, 114 packages |
| c1 | `uv lock --index https://download.pytorch.org/whl/cpu` | torch **`2.9.1+cu128`**, torchaudio `2.11.0+cu128`, **111 packages** |
| c2 | `uv lock --default-index https://download.pytorch.org/whl/cpu` | **fails**, no lock |
| c3 | `UV_INDEX_STRATEGY=unsafe-best-match UV_INDEX=…/cpu uv lock` | torch `2.11.0+cu128`, 114 packages (unchanged) |

Raw output for the two interesting cases:

```
$ uv lock --index https://download.pytorch.org/whl/cpu
Using CPython 3.12.12
Resolved 111 packages in 4.55s
    torch       2.9.1+cu128    https://download.pytorch.org/whl/cu128
    torchaudio  2.11.0+cu128   https://download.pytorch.org/whl/cu128
```

```
$ uv lock --default-index https://download.pytorch.org/whl/cpu
Using CPython 3.12.12
  × No solution found when resolving dependencies:
  ╰─▶ Because qwen-tts was not found in the package registry and your
      project depends on qwen-tts==0.1.1, we can conclude that your project's
      requirements are unsatisfiable.
```

`c1` is the important one. The flag did **not** switch torch to CPU — it kept the
`whl/cu128` source — but it perturbed resolution enough to **silently downgrade
torch from `2.11.0+cu128` to `2.9.1+cu128`** while leaving torchaudio at
`2.11.0+cu128`, a mismatched pair, and it remapped transitive packages
(`networkx` moved to `https://download.pytorch.org/whl/cpu`, three `cuda-*`
packages disappeared). An operator who added `--index` to a sync flag would get a
working-looking venv with the wrong torch and an unsound lock.

Two further data points from the real project, for completeness:

```
$ UV_DEFAULT_INDEX=https://download.pytorch.org/whl/cpu uv lock     # cpu-pinned project
  × No solution found when resolving dependencies:
  ╰─▶ Because qwen-tts was not found in the package registry and your
      project depends on qwen-tts==0.1.1, ...
$ UV_DEFAULT_INDEX=https://download.pytorch.org/whl/cu128 uv lock   # same, on the cpu variant
  × No solution found … (identical)
```

Replacing the default index removes PyPI, and a project that needs `qwen-tts` from
PyPI cannot resolve at all — so `UV_DEFAULT_INDEX` cannot be a device selector in
either direction.

**Conclusion.** The plan's premise holds with a sharper edge than stated: not only
does the source mapping win, an index override on a real engine project is
actively unsafe. The device must be part of the lock root, and the installer must
pass `--locked`.

## Experiment 4 — ROCm

### 4.1 Sourcing the index (from PyTorch, not from memory)

`https://pytorch.org/get-started/previous-versions/` (fetched 2026-11-27) documents
ROCm indexes per release. The relevant entries:

```
### v2.14.1
##### Linux and Windows
# ROCM 7.2 (Linux only)
pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/rocm7.2
# ROCM 7.14 (Linux only)
pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/rocm7.14
...
### v2.11.0
# ROCM 7.2 (Linux only)
pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/rocm7.2
```

The docs stop at v2.14.1, so the index for the current torch was confirmed from
PyTorch's own wheel host instead of guessed:

```
$ curl -sS https://download.pytorch.org/whl/ | grep -oP 'href="\K[^"/]+' | grep -i rocm
pytorch-triton-rocm
rocm10.0  rocm10.1  rocm3.10  rocm3.7  rocm3.8  rocm3.9  rocm4.0.1  rocm4.1  rocm4.2
rocm4.3.1 rocm4.5.2 rocm5.0 rocm5.1.1 rocm5.2 rocm5.3 rocm5.4.2 rocm5.5 rocm5.6 rocm5.7
rocm6.0 rocm6.1 rocm6.2 rocm6.2.4 rocm6.3 rocm6.4 rocm7.0 rocm7.1 rocm7.14 rocm7.2
rocm-preview triton-rocm
```

Only `rocm7.14` and `rocm10.0` publish `torch-2.15.0`; `rocm10.0` is not mentioned
anywhere in PyTorch's docs, so it is **not** used. `rocm7.14` is both
documented and carries the current wheels:

```
$ curl -sS https://download.pytorch.org/whl/rocm7.14/torch/ | grep -oP 'torch-2\.15\.0[^"<]*\.whl' | sort -u
torch-2.15.0%2Brocm7.14-cp311-cp311-manylinux_2_28_x86_64.whl
torch-2.15.0%2Brocm7.14-cp312-cp312-manylinux_2_28_x86_64.whl
torch-2.15.0%2Brocm7.14-cp313-cp313-manylinux_2_28_x86_64.whl
torch-2.15.0%2Brocm7.14-cp314-cp314-manylinux_2_28_x86_64.whl
torch-2.15.0%2Brocm7.14-cp314-cp314t-manylinux_2_28_x86_64.whl
torch-2.15.0%2Brocm7.14-cp315-cp315-manylinux_2_28_x86_64.whl
torch-2.15.0%2Brocm7.14-cp315-cp315t-manylinux_2_28_x86_64.whl
torch-2.15.0+rocm7.14-cp311-cp311-manylinux_2_28_x86_64.whl
torch-2.15.0+rocm7.14-cp312-cp312-manylinux_2_28_x86_64.whl
... (cp313/cp314/cp315 variants)
```

**The exact ROCm index for this repo is `https://download.pytorch.org/whl/rocm7.14`.**
Note what is *not* there: no `macosx`, no `win_amd64`, no `aarch64`.

### 4.2 Attempt 1 — `explicit = true` (the pattern the CPU variant uses)

`variants/rocm/pyproject.toml` with `url = ".../rocm7.14"`, `explicit = true`,
`[tool.uv.sources]` for torch and torchaudio:

```
$ uv lock --project /tmp/variant-test/engine/variants/rocm
Using CPython 3.12.12
Resolved 94 packages in 9.96s
```

**A lock is produced** — but it is worthless, and finding out why took three runs:

```
torch 2.0.1 <- https://download.pytorch.org/whl/rocm7.14
   markers: (all platforms)
   0 wheels
torchaudio 2.0.2 <- https://download.pytorch.org/whl/rocm7.14
   0 wheels
```

torch 2.0.1 with **zero wheels** — the lock records a package that cannot be
installed. Restricting `[tool.uv] environments` to `sys_platform == 'linux'` and
then to `linux and x86_64` changed nothing (still 2.0.1, still zero wheels).

### 4.3 Why — two independent traps

Narrowing `requires-python` to `>=3.12,<3.13` finally produced a usable error:

```
  Because only rocm[device-all]==0.1.0 is available and
  torch>=2.14.0+rocm7.14 depends on rocm[device-all]==7.14.*, we can
  conclude that torch>=2.14.0+rocm7.14 cannot be used.
  And because we know from (1) that torch<=2.0.1 cannot be used, we can
  conclude that all versions of torch cannot be used.
```

1. **`explicit = true` kills it.** torch's own runtime dependencies
   (`rocm[device-all]>=7.14.dev0`, `rocm[libraries]`, `triton-rocm`) are published
   on *the same PyTorch index*. An `explicit` index is only consulted for packages
   that name it in `[tool.uv.sources]`, so `rocm` resolved from PyPI where only
   `rocm==0.1.0` exists — unsatisfiable. The CPU/CUDA variants can be `explicit`
   because their extra dependency is nobody else's; ROCm cannot.
2. **Universal resolution backtracks to junk.** Left unrestricted, uv must satisfy
   `sys_platform == 'win32'` and `platform_machine == 'aarch64'`, and at
   `rocm7.14` the only aarch64 torch wheels belong to **2.0.1**. It picks 2.0.1,
   which has no x86-64 wheel, and writes a lock with no wheels at all. Narrowing
   `environments` is mandatory.

### 4.4 Attempt 2 — non-explicit index + linux/x86_64

```toml
[tool.uv]
environments = ["sys_platform == 'linux' and platform_machine == 'x86_64'"]

# NOT explicit: torch's own runtime deps (rocm[device-all], triton-rocm) live on
# this same PyTorch index, so an explicit index cannot serve them.
[[tool.uv.index]]
name = "pytorch-rocm7.14"
url = "https://download.pytorch.org/whl/rocm7.14"

[tool.uv.sources]
torch = [{ index = "pytorch-rocm7.14" }]
torchaudio = [{ index = "pytorch-rocm7.14" }]
```

```
$ uv lock --project /tmp/variant-test/engine/variants/rocm
Using CPython 3.12.12
Resolved 123 packages in 6.65s

torch       2.15.0+rocm7.14   <- https://download.pytorch.org/whl/rocm7.14
            2 wheels: cp311, cp312 manylinux_2_28_x86_64
torchaudio  2.11.1+rocm7.14   <- https://download.pytorch.org/whl/rocm7.14
            1 wheel: py3-none-manylinux_2_28_x86_64
```

Lock: 117 570 bytes, sha256 `1378ef073847e5ef1034b287d57aeb7bc67fb5e1322a5cf36605663a4b048ada`.
Closure includes `rocm`, `rocm-sdk-core`, 24 `rocm-sdk-device-gfx*` packages,
`rocm-sdk-libraries` and `triton-rocm`; **zero** `nvidia-*` packages.

**So: a ROCm lock *can* be produced on this Linux box, with no AMD GPU present.**
Nothing was installed. Whether it *runs* is not testable here — there is no
`/dev/kfd`, no `rocm-smi`, and `qwen-tts`'s own examples are CUDA-shaped — so
`(qwen3_tts, rocm)` remains **experimental**, not `verified`.

## Experiment 5 — MPS

`variants/mps/pyproject.toml` is the same dependency set with **no** torch source
mapping at all: the PyPI macOS arm64 torch wheel is the MPS build.

Unrestricted (universal) lock:

```
$ uv lock --project /tmp/variant-test/engine/variants/mps
Using CPython 3.12.12
Resolved 114 packages in 406ms

torch       2.15.0  <- https://pypi.org/simple
   8 wheels incl. torch-2.15.0-cp312-cp312-macosx_14_0_arm64.whl
torchaudio  2.11.1  <- https://pypi.org/simple
   incl. torchaudio-2.11.1-py3-none-macosx_14_0_arm64.whl
```

Narrowed to the only platform that has MPS:

```toml
[tool.uv]
environments = ["sys_platform == 'darwin' and platform_machine == 'arm64'"]
```

```
$ uv lock --project /tmp/variant-test/engine/variants/mps
Using CPython 3.12.12
Resolved 94 packages in 38ms

torch       2.15.0  <- https://pypi.org/simple
   2 wheels: cp311, cp312 macosx_14_0_arm64
torchaudio  2.11.1  <- https://pypi.org/simple
   1 wheel: py3-none-macosx_14_0_arm64
```

Lock: 102 009 bytes, sha256 `13c784c08c6885c54d66536879144bd12f6bd8a7325d306085ca7962ffaf8f40`.

**uv *can* lock a macOS-arm64 MPS variant from Linux** — universal resolution is
what uv does by default, so this is not surprising once measured, but it
contradicts the plan's expectation that the pair might be "not installable from
this release". It is installable; what is missing is *runtime proof*: no Mac in
this session, and the MPS path in `worker.py` (`device_map="mps"`) has never been
executed anywhere. `(qwen3_tts, mps)` stays **experimental**.

## Experiment 6 — backend claims, re-confirmed from installed metadata

All quotes are from the distributions actually installed in this checkout.

### `faster-qwen3-tts 0.5.4` — CUDA-only, no ROCm claim

`engines/qwen3_tts_fast/.venv/lib/python3.12/site-packages/faster_qwen3_tts-0.5.4.dist-info/METADATA`:

```
4:Summary: Real-time Qwen3-TTS inference with CUDA graphs or GGML
9:Keywords: tts,qwen,cuda,cudagraph,ggml,metal,streaming
40:Torch backend uses `torch.cuda.CUDAGraph`; the GGML backend supports CUDA and
46:an NVIDIA GPU with CUDA. The GGML backend also runs on Apple Silicon
```

(lines 40–46 in full: *"The Torch backend uses `torch.cuda.CUDAGraph`; the GGML
backend supports CUDA and Apple Silicon Metal."* / *"Requires: Python 3.10+ and
PyTorch 2.5.1+. The Torch backend requires an NVIDIA GPU with CUDA."*)

```
$ grep -c -i rocm .../faster_qwen3_tts-0.5.4.dist-info/METADATA
0
$ grep -c -i hip  .../faster_qwen3_tts-0.5.4.dist-info/METADATA
0
$ grep -rn -iE "rocm|hip" .../site-packages/faster_qwen3_tts/
(no matches)
```

The graph code is unconditionally CUDA:

```
faster_qwen3_tts/predictor_graph.py:189:                self.graph = torch.cuda.CUDAGraph()
faster_qwen3_tts/talker_graph.py:133:            self.graph = torch.cuda.CUDAGraph()
```

**Confirms the plan:** there is **no ROCm claim anywhere** in the installed
distribution — not in METADATA, not in the source. The "ROCm notes" comment in
`engines/qwen3_tts_fast/pyproject.toml` is unsourced and must go. The only
non-NVIDIA path the package claims is the GGML/Metal backend, which this repo does
not use.

### `fermion-research 0.2.15` — CPU / MLX / CUDA-container

`engines/phonon/.venv/lib/python3.12/site-packages/fermion_research-0.2.15.dist-info/METADATA`:

```
4:Summary: Phonon speech recognition engine and Neutrino language models from Fermion Research, on CPU, Apple silicon and NVIDIA GPUs
44:Provides-Extra: mlx
45:Requires-Dist: mlx; (sys_platform == "darwin" and platform_machine == "arm64") and extra == "mlx"
52:... Every Phonon-2 family model runs on Apple silicon through MLX, on CPUs under Linux (x86-64 and Arm),
   Windows and macOS (Apple silicon and Intel Macs), and on NVIDIA GPUs through the CUDA container.
76:On Linux, Windows and Intel Mac CPUs, that line is the whole setup: Phonon-2 runs on the CPU engine. On Linux, install torch from its CPU wheel index first, which skips the GPU build.
89:Intel Macs: Phonon-2 runs on the CPU engine (Python 3.10 to 3.12; the install resolves the last Intel torch wheel, 2.2.2, with transformers 5.0).
99:The CUDA container runs Phonon-2 on NVIDIA GPUs.
```

So the NVIDIA path is a **container** (`ghcr.io/fermionresearch/phonon-cuda:1.0.8`,
METADATA line 102), not a wheel, and the Apple silicon path is the `mlx` extra
(`FERMION_DEVICE=mlx`). Both match the plan. `(phonon, cuda)` therefore has no
in-process wheel path at all — the manifest cannot offer it as anything but
unsupported.

### `qwen-tts 0.1.1` — device-agnostic on paper, CUDA-shaped in every example

`engines/qwen3_tts/.venv/lib/python3.12/site-packages/qwen_tts-0.1.1.dist-info/METADATA`
is the upstream README. `device_map="cuda:0"` appears in **all six** code
examples: lines **187, 240, 280, 328, 345, 395**.

```
$ for t in rocm mps metal hip Apple; do grep -c -i "$t" METADATA; done
0 0 0 0 0        # none of rocm/mps/metal/hip/Apple appear anywhere
```

But `device_map` is a plain passthrough, and the package's own CLI documents
`cpu` as a valid value:

```
qwen_tts/cli/demo.py:95:        help="Device for device_map, e.g. cpu, cuda, cuda:0 (default: cuda:0).",
qwen_tts/inference/qwen3_tts_model.py:102:  Typical examples: device_map="cuda:0", dtype=torch.bfloat16, ...
```

Experiment 7 exercises that path for real: `device_map="cpu"` loads and speaks.

### `kokoro 0.9.4` — device-agnostic

`engines/kokoro/.venv/lib/python3.12/site-packages/kokoro-0.9.4.dist-info/METADATA`
has **zero** device/cuda/mps/gpu mentions (the word does not occur). The device
knob is in the API instead:

```
kokoro/pipeline.py:63:    def __init__(
kokoro/pipeline.py:70:        device: Optional[str] = None
kokoro/pipeline.py:78:            device: Override default device selection ('cuda' or 'cpu', or None for auto)
kokoro/pipeline.py:94:            if device == 'cuda' and not torch.cuda.is_available():
kokoro/pipeline.py:96:            if device is None:
kokoro/pipeline.py:97:                device = 'cuda' if torch.cuda.is_available() else 'cpu'
```

Note `pipeline.py:97`: **the default is `cuda` whenever a GPU is visible**, which
is precisely why `engines/kokoro/worker.py` must keep passing `device="cpu"` as
long as the shipped variant is CPU. It also means a `(kokoro, cuda)` variant would
be a real, cheap experiment for a later wave — the backend supports it — but it is
unverified here.

## Experiment 7 — a real `(qwen3_tts, cpu)` synthesis

Two runs. Both used the CPU torch venv from Experiment 1
(`torch 2.15.0+cpu`, `torch.version.cuda is None`), `torch.float32`, the cached
`Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` checkpoint (2.4 GB, already in
`~/.cache/huggingface/hub`), and **no reference clip** — `custom_voice` mode with
a preset speaker, which is the smallest viable path.

### 7a Model level

```python
torch.cuda.is_available()   # False   torch.version.cuda -> None
model = Qwen3TTSModel.from_pretrained(
    "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
    device_map="cpu", dtype=torch.float32, attn_implementation="sdpa",
)
wavs, sr = model.generate_custom_voice(text="Hello there.", speaker="Ryan", language="English")
```

```
torch=2.15.0+cpu cuda=None hip=None
torch.cuda.is_available()=False
torch.get_num_threads()=8
[   0.892s] import torch: 0.892s   peakRSS=216MB
********
Warning: flash-attn is not installed. Will only run the manual PyTorch version.
********
[   8.070s] import qwen_tts: 7.178s   peakRSS=525MB
[  17.195s] from_pretrained(Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice) device_map=cpu dtype=float32: 9.125s   peakRSS=5688MB
model.device = cpu
supported speakers: ['aiden', 'dylan', 'eric', 'ono_anna', 'ryan', 'serena', 'sohee', 'uncle_fu', 'vivian']
supported languages: ['auto', 'chinese', 'english', 'french', 'german', 'italian', 'japanese', 'korean', 'portuguese', 'russian', 'spanish']
[  29.447s] generate_custom_voice 'short' (12 chars): 12.252s   peakRSS=5688MB
{"text": "Hello there.", "sample_rate": 24000, "samples": 57600, "audio_seconds": 2.4,
 "generate_seconds": 12.252, "realtime_multiple": 0.1959, "peak_abs_amplitude": 0.4229,
 "wav_path": "/tmp/variant-test/cpu-tts-out/qwen-cpu-short.wav", "wav_bytes": 115244}
```

`/usr/bin/time -v`: `Maximum resident set size (kbytes): 5824980` (**5.55 GiB**),
`Percent of CPU this job got: 342%`, `User time: 96.74 s`, `Elapsed: 0:30.55`.

```
$ ffprobe -v error -show_entries format=duration,size -show_entries stream=sample_rate,channels,codec_name \
    -of default=noprint_wrappers=1 /tmp/variant-test/cpu-tts-out/qwen-cpu-short.wav
codec_name=pcm_s16le  sample_rate=24000  channels=1  duration=2.400000  size=115244
$ ffmpeg -hide_banner -i …/qwen-cpu-short.wav -af volumedetect -f null -
mean_volume: -23.4 dB
max_volume: -7.5 dB
```

### 7b Worker level, over real IPC

The **shipped worker** hardcodes the accelerator:

```
engines/qwen3_tts/worker.py:193-200
        model = Qwen3TTSModel.from_pretrained(
            repo,
            device_map="cuda:0",
            dtype=torch.bfloat16,
```

so it cannot run on CPU unmodified. In the temp copy only, that block was changed
to read `AGENT_VOICE_RESOLVED_DEVICE` (`cpu`→`"cpu"`/`float32`,
`mps`→`"mps"`/`float32`, else `"cuda:0"`/`bfloat16`), `_vram_mb()` was made to
return `None` off cuda/rocm, and `device` was added to `hello` — i.e. the three
smallest Wave 2 edits. Nothing in the repo was touched.

The worker was then driven over a real `AF_UNIX` socket with the protocol from
`src/agent_voice/ipc/protocol.py` (`>I` length + 1 type byte + payload):

```
[   0.024s] worker spawned and connected pid=465519
hello -> {"backend_name": "qwen-tts", "backend_version": "0.1.1",
          "capabilities": {"custom_voice": true, "instruct_control": true,
                           "pcm_native": false, "streaming": false, "voice_design": true},
          "device": "cpu", "engine_id": "qwen3_tts", "gpu_vram_mb": null,
          "loaded_models": [], "pid": 465519, "protocol_version": 1}
[  10.209s] load_model -> {"id": "9bd00b44-…", "loaded_models": ["0.6b-custom"], "model_alias": "0.6b-custom"}
health -> {"gpu_vram_mb": null, "id": "832273be-…", "in_flight": 0,
           "loaded_models": ["0.6b-custom"], "ok": true}
[   7.985s] synthesize -> {"backend_name": "qwen-tts", "backend_version": "0.1.1",
           "duration_sec": 1.76, "id": "f234f661-…", "model_alias": "0.6b-custom",
           "sample_rate": 24000, "wav_path": "/tmp/variant-test/cpu-tts-out/worker-cpu.wav"}
shutdown -> {"id": "d1b01b90-…", "ok": true}
{"device": "cpu", "synthesize_wall_s": 7.985, "wav_bytes": 84524}
```

`/usr/bin/time -v`: `Maximum resident set size: 5830072 kB` (**5.56 GiB**),
`Percent of CPU this job got: 379%`, `User time: 69.26 s`, `Elapsed: 0:19.28`.

```
$ ffprobe … /tmp/variant-test/cpu-tts-out/worker-cpu.wav
codec_name=pcm_s16le  sample_rate=24000  channels=1  duration=1.760000  size=84524
$ ffmpeg -af volumedetect …
mean_volume: -19.9 dB
max_volume: -3.7 dB
```

### 7c What this measures

| Quantity | Value |
|---|---:|
| Model load, `device_map="cpu"`, `float32`, weights cached | **9.1 s** (in-worker: 10.2 s incl. interpreter + qwen_tts import) |
| `import torch` / `import qwen_tts` | 0.89 s / 7.18 s |
| Generate, 12-character text → 2.40 s audio | **12.25 s** → **0.196× realtime** (~5.1× slower than realtime) |
| Generate via the worker, same text → 1.76 s audio | **7.99 s** → 0.22× realtime |
| Peak RSS, model resident | **5.55–5.56 GiB** |
| `gpu_vram_mb` reported | **null** (not 0 — there is no CUDA allocator) |
| Output | valid 24 kHz mono PCM s16le, 2.40 s / 1.76 s, mean −23.4 / −19.9 dB |

That is the honest number: **CPU qwen3_tts is usable for short utterances and
roughly five times slower than realtime**, on a 5700X at ~3.4–3.8 cores. No
speed multiplier is invented for `recommend_profile`; "slow on CPU" is the claim
the measurement supports.

## Experiment 8 (unplanned) — switching device in place

Because the plan asserts "switching a device reinstalls that engine's venv", the
two variants were synced alternately into the **same** canonical `.venv`:

```
BEFORE: torch 2.15.0+cpu (cpu variant), venv 1.6G

$ UV_PROJECT_ENVIRONMENT=$E/.venv uv sync --project $E --locked
 + torch==2.11.0+cu128
 - torchaudio==2.11.1+cpu
 + torchaudio==2.11.0+cu128
 + triton==3.6.0
 + typer==0.29.2
real 0m0.425s          (uv cache warm)
AFTER: 2.11.0+cu128 / 2.11.0+cu128, venv 7.3G

$ UV_PROJECT_ENVIRONMENT=$E/.venv uv sync --project $E/variants/cpu --locked
 - torch==2.11.0+cu128
 + torch==2.15.0+cpu
 - torchaudio==2.11.0+cu128
 + torchaudio==2.11.1+cpu
 - triton==3.6.0
real 0m0.330s
AFTER: 2.15.0+cpu / 2.11.1+cpu, venv 1.6G, zero nvidia_/triton dirs left behind
```

So a device switch is a **content swap**, not a delete-and-recreate: `uv sync`
installs and prunes to exactly the variant's lock, and the canonical venv path
never moves. On a cold cache the same swap is a full download — the venv grows
from 1.6 GB to 7.3 GB — which `setup --dry-run` should surface as the download
size, and `engines install --device` should not pretend is instant.

## Not verified here

Everything below is **unmeasured in this session** and must not be described as
verified in the README matrix or `docs/engines.md`:

- `(kokoro, cpu)`, `(phonon, cpu)`, `(qwen3_tts, cuda)`, `(qwen3_tts_fast, cuda)` —
  these are carried forward from `docs/phase0-kokoro-report.md`,
  `docs/phase0-asr-report.md` and `docs/phase0-report.md:70-85` plus the existing
  engine tests. They were **not re-run** for this report.
- Any ROCm *runtime*: no AMD GPU, no `/dev/kfd`, no `rocm-smi`, no `rocminfo` on
  this box. The ROCm lock was produced; nothing was installed from it.
- Any MPS *runtime*: no Apple silicon. The MPS lock was produced on Linux.
- `qwen3_tts` on `cuda`/`rocm`/`mps` with the v0.6.0 device plumbing — only the
  `cpu` branch of the temp-copy patch was executed.
- The GPU path of the temp-copy worker: `gpu_vram_mb: null` was only observed with
  no CUDA build installed. The cuda branch of `_vram_mb()` is untested here.
- `faster-qwen3-tts` on CPU/MPS/ROCm: not attempted. Its metadata says CUDA-only
  for the Torch backend (Experiment 6), and its CUDA-graph code is unconditional.
- `fermion`/phonon on `mps` (MLX) or `cuda`: not attempted.
- `qwen-tts` with `torch.bfloat16` on CPU: not attempted (float32 only, as
  specified).
- Behaviour of `uv sync --locked` when the venv is **missing** and the interpreter
  is not the host default: only CPython 3.12.12 was exercised.
- Windows: nothing at all.

## Decision table

Levels mean: **verified here** = installed and driven end to end on this host (or
proven by a prior measured report, named); **experimental** = the package plausibly
supports it, the lock is producible, but no run was observed; **unsupported** =
package metadata or code says it cannot work.

| Pair | Level | Why |
|---|---|---|
| `(kokoro, cpu)` | verified here | `docs/phase0-kokoro-report.md`; engine tests; torch 2.15.0+cpu lock at `engines/kokoro/uv.lock:28-29`; worker passes `device="cpu"` (`engines/kokoro/worker.py:282`) and reports it (`:442,466,498`). Not re-run in this session. |
| `(phonon, cpu)` | verified here | `docs/phase0-asr-report.md`: 42–48× realtime, ~1.0 GB RSS, 0 MB VRAM. Not re-run in this session. |
| `(mock, cpu)`, `(mock_asr, cpu)` | verified here | no dependencies, no torch; `mock_asr` already declares `device: cpu`. |
| `(qwen3_tts, cuda)` | verified here | `docs/phase0-report.md:70-85`; committed lock resolves `2.11.0+cu128` from `whl/cu128`; existing GPU gate + `engines test qwen3_tts`. Not re-run in this session. |
| `(qwen3_tts_fast, cuda)` | verified here | `docs/phase0-report.md:70-85`: both CUDA graphs captured on this GPU/driver. Not re-run in this session. |
| **`(qwen3_tts, cpu)`** | **verified here** | **Experiment 7**: real synthesis at model level *and* through the worker over IPC, `torch 2.15.0+cpu`, `float32`, valid 24 kHz audio (2.40 s / 1.76 s, mean −23.4 / −19.9 dB), 0.196–0.22× realtime, 5.55 GiB peak RSS. Requires the Wave 2 `device_map` change; the committed worker hardcodes `cuda:0` (`worker.py:193-200`). |
| `(qwen3_tts, mps)` | experimental | Lock producible from Linux (94 packages, `torch 2.15.0` `macosx_14_0_arm64`). No Mac; `device_map="mps"` never executed. |
| `(qwen3_tts, rocm)` | experimental | Lock producible (`torch 2.15.0+rocm7.14`, 123 packages) with a **non-explicit** index + `linux`/`x86_64` environments. No AMD GPU; nothing installed or run. |
| `(kokoro, cuda)` | experimental | `KPipeline(device="cuda")` exists (`kokoro/pipeline.py:70,78`) but no CUDA torch build was installed for kokoro; 0 MB assertions are CPU-only. |
| `(kokoro, mps)` | experimental | `pipeline.py:78` documents only `'cuda'` or `'cpu'`; `mps` is not a documented value. Untested; must not ship without a mocked test at minimum. |
| `(phonon, mps)` | experimental | `fermion-research[mlx]` extra exists (METADATA lines 44–47) and maps to `FERMION_DEVICE=mlx`; never executed on Apple silicon. |
| `(qwen3_tts_fast, cpu)` | unsupported | METADATA line 46: "The Torch backend requires an NVIDIA GPU with CUDA"; `torch.cuda.CUDAGraph` at `predictor_graph.py:189`, `talker_graph.py:133`. |
| `(qwen3_tts_fast, mps)` | unsupported | same; the package's non-NVIDIA path is GGML/Metal, which this repo does not ship. |
| `(qwen3_tts_fast, rocm)` | unsupported | **no ROCm or HIP claim anywhere** in METADATA or source (Experiment 6). The unsourced "ROCm notes" comment in its pyproject must be deleted. |
| `(phonon, cuda)` | unsupported | NVIDIA support is the `ghcr.io/fermionresearch/phonon-cuda` **container** (METADATA line 99–102), not a wheel; `fermion.load_speech` has no CUDA device kwarg to pass. |
| `(mock* , cuda/mps/rocm)` | unsupported | CPU-only by construction; `gpu_vram_mb=0` always. |

## Recommendation for the variant mechanism

**Ship the plan's mechanism as written; do not use the fallback.**

```
engines/<id>/
├── pyproject.toml        # canonical variant, committed uv.lock, [tool.agent-voice] device = "<default>"
├── uv.lock
├── .venv                 # the one venv, created and populated by the sync below
└── variants/<device>/
    ├── pyproject.toml    # same deps, different torch index, [tool.agent-voice] device = "<device>"
    └── uv.lock
```

`engines install <id> --device <d>` must run exactly:

```python
subprocess.run(
    [uv, "sync", "--project", str(project_dir), "--locked"],
    env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(engine_dir / ".venv")},
    cwd=str(project_dir),
    shell=False,
    check=True,
)
```

Evidence for each requirement:

| Requirement | Evidence |
|---|---|
| `UV_PROJECT_ENVIRONMENT` puts the venv at the canonical path | Exp 1.4: venv created at `engine/.venv`, no `.venv` under `variants/cpu/`, `pyvenv.cfg` prompt `agent-voice-engine-qwen3-tts` |
| `--project variants/<device>` is enough to select the lock root | Exp 1.2/1.3: lock written beside the variant pyproject; canonical lock untouched |
| `--locked` is mandatory | Exp 2: refusal (exit 1) with `--locked`, silent rewrite without it |
| the canonical `uv.lock` is never touched | Exp 1.5: byte-identical sha256 **and** mtime; `git status --short engines/` empty |
| switching device swaps the venv contents safely | Exp 8: cuda↔cpu in place, 0.33–0.43 s warm, exact prune, no leftover `nvidia_*` |
| `VIRTUAL_ENV` in the caller's environment is harmless | Exp 1.3: warning only; `UV_PROJECT_ENVIRONMENT` wins |

**Firewall the flags.** `engines install` must never pass `--index`,
`--default-index`, `--index-url`, `UV_INDEX` or `UV_DEFAULT_INDEX`. Experiment 3
shows they cannot select a device and, on `qwen3_tts`, `--index` silently
downgrades torch and desynchronises torchaudio. Strip those keys from the
environment before spawning `uv` (they may be set by a user's shell for other
projects), just as the worker strips `CUDA_VISIBLE_DEVICES` for CPU engines.

**Two variant-authoring rules the plan does not have yet.** Add them to
`engines/ADDING_AN_ENGINE.md` and enforce in review:

1. A variant whose wheel index is **not** PyPI must declare
   `[tool.uv] environments` for the platforms that index actually serves
   (`["sys_platform == 'linux' and platform_machine == 'x86_64'"]` for ROCm,
   `["sys_platform == 'darwin' and platform_machine == 'arm64'"]` for MPS).
   Without it, uv's universal resolution backtracks to an ancient torch that has
   broad wheel coverage — measured: `rocm7.14` → `torch 2.0.1`, zero wheels.
2. Only an index that serves **nothing but** torch/torchaudio may be
   `explicit = true`. ROCm must be non-explicit, because torch's `rocm[device-all]`
   and `triton-rocm` dependencies live on the same index; a non-explicit index
   must therefore be checked per package (Experiment 3 shows an extra index can
   remap a transitive dependency such as `networkx`).

**What not to ship.** Do not ship a ROCm or MPS variant as *available* until one
has been installed and run on real hardware. Per the plan's own gate, a pair whose
lock cannot be produced is unavailable; ROCm and MPS locks **can** be produced, so
the honest gate is the runtime one: `engines install --device rocm|mps` should
require `--experimental`, and `recommend_profile` must never select them
automatically. `(qwen3_tts_fast, rocm)` and `(phonon, cuda)` stay refused
unconditionally, `--experimental` or not.

## Corrections to the approved plan

1. **"`(qwen3_tts, cpu)` verified only if experiment 7 ran" — resolved: it ran.**
   Model level and worker level, real audio. The one caveat the plan should carry
   into Wave 2: the committed worker cannot execute it (`worker.py:193-200`
   hardcodes `device_map="cuda:0"`, `dtype=torch.bfloat16`), so the pair is
   "verified with the Wave 2 change", and the measurement is 0.196–0.22× realtime
   at **5.55 GiB RSS**, not the ~2 GB a 0.6B bf16 model would suggest.
2. **"MPS … if uv cannot lock it, record that and mark the pair 'experimental,
   not installable from this release'" — uv *can* lock it.** 94 packages from
   Linux, `torch 2.15.0` `macosx_14_0_arm64`. The pair is installable; it is the
   runtime that is unproven. Do not write "not installable" in the README.
3. **"Do not invent a ROCm URL … if it cannot be sourced/proven, the pair is not
   installable" — it can be sourced.** `https://download.pytorch.org/whl/rocm7.14`
   is documented on `pytorch.org/get-started/previous-versions` and serves
   `torch-2.15.0+rocm7.14`. But the plan's implicit assumption that a ROCm variant
   is authored like the CPU one (`explicit = true`) is **wrong** and produces a
   lock with zero wheels; the two rules in the recommendation above are required.
4. **`[tool.agent-voice]` does not exist in any engine pyproject today**
   (`grep -rn "tool.agent-voice" engines/*/pyproject.toml` → no matches). The
   device-resolution step in `engines install` has nothing to compare against in
   v0.5.0 and must tolerate its absence (fall back to "canonical = the variant the
   committed lock was built from"), not assume it.
5. **The `VIRTUAL_ENV` interaction is real** and should be in `docs/troubleshooting.md`:
   a user who has activated another venv gets
   `warning: VIRTUAL_ENV=… does not match the project environment path`. Harmless,
   but confusing; `engines install` should say so or `pop` the key.
6. **torch/torchaudio versions differ between variants** —
   `2.11.0+cu128`/`2.11.0+cu128` on CUDA vs `2.15.0+cpu`/`2.11.1+cpu` on CPU.
   `docs/engines.md` must not claim the variants hold the same torch, and
   `engines/<id>/.variant` should record both stamps so the manager's torch probe
   can report something truthful.

Sources: [PyTorch previous versions](https://pytorch.org/get-started/previous-versions/),
[PyTorch wheel index root](https://download.pytorch.org/whl/),
[PyTorch CPU wheel index](https://download.pytorch.org/whl/cpu),
[PyTorch ROCm 7.14 wheel index](https://download.pytorch.org/whl/rocm7.14),
[uv project environments](https://docs.astral.sh/uv/concepts/projects/config/#project-environment-path),
[uv indexes](https://docs.astral.sh/uv/concepts/indexes/).
