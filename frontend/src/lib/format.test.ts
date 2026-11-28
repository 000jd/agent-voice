import { expect, test } from 'vitest';
import {
	formatBytes,
	formatClock,
	formatDuration,
	formatGpuLabel,
	formatTimestamp,
	formatVram,
	plural,
	relativeTime,
	srtTimestamp
} from '#lib/format.js';
import { makeClip, makeGpu } from '#lib/test-fixtures.js';

test('formats clocks, timestamps and sizes consistently', () => {
	expect(formatClock(1.2)).toBe('0:01.20');
	expect(formatClock(61.25)).toBe('1:01.25');
	expect(formatClock(3661.2)).toBe('1:01:01.20');
	expect(formatDuration(61.9)).toBe('1:01');
	expect(formatDuration(3661)).toBe('1:01:01');
	expect(formatTimestamp(1.2)).toBe('00:01.20');
	expect(srtTimestamp(1.2)).toBe('00:00:01,200');
	expect(formatVram(3584)).toBe('3.5 GB');
	expect(formatVram(512)).toBe('512 MB');
	expect(formatBytes(1536)).toBe('1.5 KB');
	expect(formatBytes(3 * 1024 ** 3)).toBe('3.00 GB');
	expect(plural(1, 'take')).toBe('1 take');
	expect(plural(2, 'take')).toBe('2 takes');
});

test('summarises GPU state without inventing a value', () => {
	const gpu = makeGpu({
		activeEngine: 'mock',
		loadedModels: ['mock-tone'],
		workers: { mock: 'alive' },
		workerDevices: { mock: 'cpu' },
		workerVramMb: { mock: 0 },
		vramSource: 'nvml',
		vramUsedMb: 0,
		vramFreeMb: 1024
	});
	expect(formatGpuLabel(gpu)).toBe('mock · 0 MB');
	expect(formatGpuLabel(makeGpu({ cpuEngines: ['mock'] }))).toBe('CPU · 0 MB');
	expect(formatGpuLabel(makeGpu())).toBe('Idle · 0 MB');
	expect(makeClip().cached).toBe(false);
});

test('renders relative dates without a locale dependency', () => {
	expect(relativeTime(0)).toBe('—');
	expect(relativeTime(1000, 1030)).toBe('just now');
	expect(relativeTime(1000, 1000 + 120)).toBe('2m ago');
	expect(relativeTime(1000, 1000 + 7200)).toBe('2h ago');
	expect(relativeTime(1000, 1000 + 172_800)).toBe('2d ago');
});
