import type { ClipSummary, GpuStatus, TranscribeSegment } from '#lib/api/types.js';

export function clamp(value: number, min: number, max: number): number {
	return Math.min(max, Math.max(min, value));
}

export function formatClock(seconds: number, decimals = 2): string {
	const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
	const hours = Math.floor(safe / 3600);
	const minutes = Math.floor((safe % 3600) / 60);
	const secs = safe % 60;
	const width = decimals > 0 ? 3 + decimals : 2;
	const body = secs.toFixed(decimals).padStart(width, '0');
	if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${body}`;
	return `${minutes}:${body}`;
}

export function formatTimestamp(seconds: number): string {
	const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
	const centiseconds = Math.round(safe * 100);
	const hundredths = centiseconds % 100;
	const totalSeconds = Math.floor(centiseconds / 100);
	const hours = Math.floor(totalSeconds / 3600);
	const minutes = Math.floor((totalSeconds % 3600) / 60);
	const secs = totalSeconds % 60;
	const base = `${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}.${String(hundredths).padStart(2, '0')}`;
	return hours > 0 ? `${String(hours).padStart(2, '0')}:${base}` : base;
}

export function formatShortDuration(seconds: number): string {
	if (!Number.isFinite(seconds) || seconds <= 0) return '0.0s';
	return `${seconds.toFixed(1)}s`;
}

export function formatVram(mb: number | null | undefined): string {
	if (mb === null || mb === undefined || !Number.isFinite(mb)) return '—';
	if (mb <= 0) return '0 MB';
	if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
	return `${Math.round(mb)} MB`;
}

export function formatRtf(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value) || value <= 0) return '—';
	return `${value.toFixed(1)}× RT`;
}

export function formatElapsed(ms: number): string {
	if (!Number.isFinite(ms) || ms < 0) return '—';
	if (ms < 1000) return `${Math.round(ms)} ms`;
	return `${(ms / 1000).toFixed(1)} s`;
}

export function formatBytes(bytes: number): string {
	if (!Number.isFinite(bytes) || bytes < 0) return '—';
	if (bytes < 1024) return `${bytes} B`;
	if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
	if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
	return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export function formatDate(epochSeconds: number): string {
	if (!Number.isFinite(epochSeconds) || epochSeconds <= 0) return '—';
	return new Intl.DateTimeFormat('en', {
		month: 'short',
		day: 'numeric',
		year: 'numeric'
	}).format(new Date(epochSeconds * 1000));
}

export function formatGpuLabel(gpu: GpuStatus | null): string {
	if (!gpu) return 'GPU —';
	const used = gpu.vramUsedMb ?? sumVram(gpu);
	if (gpu.activeEngine) return `${shortEngine(gpu.activeEngine)} · ${formatVram(used)}`;
	if (gpu.loadedModels.length > 0) {
		return `${gpu.loadedModels.length} loaded · ${formatVram(used)}`;
	}
	if (gpu.cpuEngines.length > 0) return `CPU · 0 MB`;
	return `Idle · ${formatVram(used)}`;
}

function sumVram(gpu: GpuStatus): number {
	let total = 0;
	for (const value of Object.values(gpu.workerVramMb)) {
		if (typeof value === 'number' && Number.isFinite(value)) total += value;
	}
	return total;
}

export function shortEngine(engineId: string): string {
	return engineId.length > 20 ? `${engineId.slice(0, 18)}…` : engineId;
}

export function cacheBadge(clip: ClipSummary): string {
	if (clip.cached) return `Cached · ${formatElapsed(clip.elapsedMs)}`;
	if (clip.realtimeMultiple && clip.realtimeMultiple > 0) return formatRtf(clip.realtimeMultiple);
	return formatElapsed(clip.elapsedMs);
}

export function shortText(text: string, max = 72): string {
	const normalized = text.replace(/\s+/g, ' ').trim();
	if (normalized.length <= max) return normalized;
	return `${normalized.slice(0, max - 1).trimEnd()}…`;
}

export function sanitizeFilename(name: string): string {
	const cleaned = name
		.replace(/[^\w.-]+/g, '-')
		.replace(/^-+|-+$/g, '')
		.slice(0, 64);
	return cleaned || 'agent-voice-audio';
}

export function toSrt(segments: TranscribeSegment[]): string {
	return segments
		.map((segment, index) => {
			return [
				String(index + 1),
				`${srtTimestamp(segment.startSec)} --> ${srtTimestamp(segment.endSec)}`,
				segment.text.trim(),
				''
			].join('\n');
		})
		.join('\n');
}

export function toVtt(segments: TranscribeSegment[]): string {
	const body = segments
		.map((segment) => {
			return `${vttTimestamp(segment.startSec)} --> ${vttTimestamp(segment.endSec)}\n${segment.text.trim()}\n`;
		})
		.join('\n');
	return `WEBVTT\n\n${body}`;
}

function srtTimestamp(seconds: number): string {
	const safe = Math.max(0, seconds);
	const totalMilliseconds = Math.round(safe * 1000);
	const milliseconds = totalMilliseconds % 1000;
	const totalSeconds = Math.floor(totalMilliseconds / 1000);
	const hours = Math.floor(totalSeconds / 3600);
	const minutes = Math.floor((totalSeconds % 3600) / 60);
	const secs = totalSeconds % 60;
	return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')},${String(milliseconds).padStart(3, '0')}`;
}

function vttTimestamp(seconds: number): string {
	return srtTimestamp(seconds).replace(',', '.');
}

export function downloadText(filename: string, text: string, mime: string): void {
	const blob = new Blob([text], { type: mime });
	const url = URL.createObjectURL(blob);
	const link = document.createElement('a');
	link.href = url;
	link.download = filename;
	link.click();
	window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
