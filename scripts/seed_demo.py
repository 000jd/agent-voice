#!/usr/bin/env python3
"""Populate an isolated Studio daemon with reviewable demo data.

Requires the daemon to run with the bundled mock TTS/ASR engines. It uses only
the public REST API, so it also exercises the same paths the browser uses.
"""

from __future__ import annotations

import argparse
import io
import sys

import httpx
import numpy as np
import soundfile as sf

VOICE_NAMES = ("Demo Ada", "Demo Rowan", "Demo Kit")
PRESET_LINES = (
    "A measured line for the late session.",
    "The room is quiet, and the first take is ready.",
    "Small details make a voice feel present.",
)
DESIGN_NAME = "Demo Brass"


def wav_bytes(seconds: float = 4.0) -> bytes:
    rate = 24_000
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    signal = 0.16 * np.sin(2 * np.pi * 180 * t) + 0.07 * np.sin(2 * np.pi * 420 * t)
    buffer = io.BytesIO()
    sf.write(buffer, signal.astype(np.float32), rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    client = httpx.Client(base_url=args.base_url, timeout=120.0)
    try:
        health = client.get("/health")
        health.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"daemon not reachable at {args.base_url}: {exc}", file=sys.stderr)
        return 1

    voices = {voice["name"] for voice in client.get("/v1/voices").json()["voices"]}
    clip = wav_bytes()
    for name in VOICE_NAMES:
        if name in voices:
            continue
        draft = client.post(
            "/v1/voice-drafts",
            files={"file": ("demo.wav", clip, "audio/wav")},
        )
        if draft.status_code != 201:
            print(f"draft failed for {name}: {draft.status_code} {draft.text}", file=sys.stderr)
            return 1
        created = client.post(
            "/v1/voices",
            json={
                "name": name,
                "draft_id": draft.json()["draft_id"],
                "trim": {"start_sec": 0.2, "end_sec": 3.6},
                "ref_text": "A short, clean demo reference for the Studio.",
                "consent": True,
                "language": "English",
            },
        )
        if created.status_code not in {200, 409}:
            print(f"voice failed for {name}: {created.status_code} {created.text}", file=sys.stderr)
            return 1

    if DESIGN_NAME not in {voice["name"] for voice in client.get("/v1/voices").json()["voices"]}:
        designed = client.post(
            "/v1/voices/design",
            json={
                "name": DESIGN_NAME,
                "instruct": "Warm, measured, late-night studio narration.",
                "sample_text": "The studio is quiet, and the first take is ready to hear.",
                "language": "English",
                "save_as_clone": True,
                "candidates": 1,
                "overwrite": True,
                "engine": "mock",
                "model": "mock-design",
                "quality": "high",
            },
        )
        if designed.status_code != 200:
            print(f"design failed: {designed.status_code} {designed.text}", file=sys.stderr)
            return 1

    history = client.get("/v1/history", params={"limit": 200}).json()["items"]
    history_text = {item["text"] for item in history}
    for index, line in enumerate(PRESET_LINES):
        if line in history_text:
            continue
        response = client.post(
            "/v1/agent/speak",
            json={
                "text": line,
                "speaker": "Ryan" if index % 2 == 0 else "Serena",
                "engine": "mock",
                "quality": "fast",
            },
        )
        if response.status_code != 200:
            print(f"speak failed: {response.status_code} {response.text}", file=sys.stderr)
            return 1

    captures = client.get("/v1/captures", params={"limit": 200}).json()["items"]
    if not captures:
        response = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("demo.wav", clip, "audio/wav")},
            data={"model": "mock-asr-parity", "response_format": "verbose_json"},
        )
        if response.status_code != 200:
            print(f"transcription failed: {response.status_code} {response.text}", file=sys.stderr)
            return 1

    print(
        f"demo ready: {len(client.get('/v1/voices').json()['voices'])} voices, "
        f"{len(client.get('/v1/history', params={'limit': 200}).json()['items'])} history, "
        f"{len(client.get('/v1/captures', params={'limit': 200}).json()['items'])} captures"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
