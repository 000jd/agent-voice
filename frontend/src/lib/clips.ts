/** Convert API results into the one player clip shape. */
import type {
	CaptureSummary,
	ClipSummary,
	HistoryItem,
	SynthesisResult,
	VoicePreview
} from '#lib/api/types.js';
import { shortText } from '#lib/format.js';

export function clipFromSynthesis(result: SynthesisResult, text: string): ClipSummary {
	return {
		id: result.artifactId,
		title: shortText(text, 56) || 'Untitled take',
		subtitle: `${result.engineId} · ${result.modelAlias}`,
		url: result.downloadUrl || `/audio/${result.artifactId}`,
		durationSec: result.durationSec,
		engineId: result.engineId,
		modelAlias: result.modelAlias,
		cached: result.cached,
		elapsedMs: result.elapsedMs,
		realtimeMultiple: result.realtimeMultiple,
		format: result.format,
		createdAt: Date.now() / 1000,
		voiceId: result.voiceId,
		speaker: result.speaker,
		mode: result.mode
	};
}

export function clipFromHistory(item: HistoryItem): ClipSummary | null {
	if (!item.audioUrl) return null;
	return {
		id: item.artifactId ?? item.id,
		title: shortText(item.text, 56) || `${item.kind} take`,
		subtitle: [item.voiceName || item.speaker || item.voiceId, item.engineId]
			.filter(Boolean)
			.join(' · '),
		url: item.audioUrl,
		durationSec: item.durationSec,
		engineId: item.engineId,
		modelAlias: item.modelAlias,
		cached: false,
		elapsedMs: 0,
		realtimeMultiple: null,
		format: item.format,
		createdAt: item.createdAt,
		voiceId: item.voiceId,
		speaker: item.speaker,
		mode: item.kind === 'design' ? 'voice_design' : item.speaker ? 'custom_voice' : 'clone'
	};
}

export function clipFromPreview(preview: VoicePreview, title: string): ClipSummary {
	return {
		id: preview.artifactId,
		title: `Preview · ${title}`,
		subtitle: `${preview.engineId} · ${preview.modelAlias}`,
		url: preview.downloadUrl,
		durationSec: preview.durationSec,
		engineId: preview.engineId,
		modelAlias: preview.modelAlias,
		cached: preview.cached,
		elapsedMs: 0,
		realtimeMultiple: null,
		format: 'wav',
		createdAt: Date.now() / 1000,
		voiceId: preview.voiceId,
		speaker: preview.speaker,
		mode: preview.speaker ? 'custom_voice' : 'clone'
	};
}

export function clipFromCapture(capture: CaptureSummary): ClipSummary {
	return {
		id: `capture:${capture.id}`,
		title: capture.title || 'Capture',
		subtitle: `${capture.engineId} · ${capture.modelAlias}`,
		url: capture.audioUrl || `/v1/captures/${capture.id}/audio`,
		durationSec: capture.durationSec,
		engineId: capture.engineId,
		modelAlias: capture.modelAlias,
		cached: false,
		elapsedMs: 0,
		realtimeMultiple: null,
		format: 'wav',
		createdAt: capture.createdAt,
		voiceId: null,
		speaker: null,
		mode: 'clone'
	};
}
