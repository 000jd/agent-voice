import { expect, test } from 'vitest';
import {
	formatClock,
	formatGpuLabel,
	formatRtf,
	formatTimestamp,
	formatVram,
	toSrt,
	toVtt
} from '#lib/format.js';
import { makeClip } from '#lib/test-fixtures.js';
import type { GpuStatus, TranscribeSegment } from '#lib/api/types.js';

const segments: TranscribeSegment[] = [
	{ index: 0, startSec: 1.2, endSec: 4.8, text: 'First line', words: [] },
	{ index: 1, startSec: 4.8, endSec: 8.1, text: 'Second line', words: [] }
];

test('formats clocks, timestamps and sizes consistently', () => {
	expect(formatClock(1.2)).toBe('0:01.20');
	expect(formatClock(61.25)).toBe('1:01.25');
	expect(formatClock(3661.2)).toBe('1:01:01.20');
	expect(formatTimestamp(1.2)).toBe('00:01.20');
	expect(formatVram(3584)).toBe('3.5 GB');
	expect(formatVram(512)).toBe('512 MB');
	expect(formatRtf(1.8)).toBe('1.8× RT');
	expect(formatRtf(null)).toBe('—');
});

test('summarises GPU state without inventing a value', () => {
	const gpu: GpuStatus = {
		activeEngine: 'mock',
		loadedModels: ['mock-tone'],
		workers: { mock: 'alive' },
		cpuEngines: [],
		workerDevices: { mock: 'cpu' },
		workerVramMb: { mock: 0 },
		vramSource: 'nvml',
		vramUsedMb: 0,
		vramFreeMb: 1024,
		queueDepth: 0,
		inFlight: 0,
		unloadInSeconds: null,
		lastUsedAt: null
	};
	expect(formatGpuLabel(gpu)).toBe('mock · 0 MB');
	expect(formatGpuLabel(null)).toBe('GPU —');
	expect(makeClip().cached).toBe(false);
});

test('serialises timed segments as SRT and WebVTT', () => {
	const srt = toSrt(segments);
	expect(srt).toContain('00:00:01,200 --> 00:00:04,800');
	expect(srt).toContain('First line');
	const vtt = toVtt(segments);
	expect(vtt.startsWith('WEBVTT\n\n')).toBe(true);
	expect(vtt).toContain('00:00:04.800 --> 00:00:08.100');
});
