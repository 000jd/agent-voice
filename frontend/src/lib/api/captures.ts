/** Transcription captures: upload/transcribe, list, detail, edit, export. */
import { api, apiFetch, readJsonBody, requestBlob, unwrap, ApiError } from './client.js';
import {
	ApiShapeError,
	parseCaptureDetail,
	parseCapturePage,
	parseErrorPayload,
	parseTranscribeResult,
	parseVoiceDraft
} from './guards.js';
import type { CaptureDetail, CapturePage, TranscribeResult, VoiceDraft } from './types.js';
import { uploadForm, type UploadTask } from './uploads.js';

export type CaptureQuery = {
	limit?: number;
	cursor?: string | null;
	q?: string | null;
};

export async function listCaptures(query: CaptureQuery = {}): Promise<CapturePage> {
	const params: Record<string, string | number> = {};
	if (query.limit) params.limit = query.limit;
	if (query.cursor) params.cursor = query.cursor;
	if (query.q?.trim()) params.q = query.q.trim();
	const result = (await api['/v1/captures'].GET({
		params: { query: params }
	})) as Parameters<typeof unwrap>[0];
	return parseCapturePage(unwrap(result, '/v1/captures'));
}

export async function getCapture(captureId: string): Promise<CaptureDetail> {
	const result = (await api['/v1/captures/{capture_id}'].GET({
		params: { path: { capture_id: captureId } }
	})) as Parameters<typeof unwrap>[0];
	return parseCaptureDetail(unwrap(result, `/v1/captures/${captureId}`));
}

export async function patchCapture(
	captureId: string,
	patch: { transcript?: string; title?: string }
): Promise<CaptureDetail> {
	const result = (await api['/v1/captures/{capture_id}'].PATCH({
		params: { path: { capture_id: captureId } },
		body: {
			transcript: patch.transcript ?? null,
			title: patch.title ?? null
		}
	})) as Parameters<typeof unwrap>[0];
	return parseCaptureDetail(unwrap(result, `/v1/captures/${captureId}`));
}

export async function deleteCapture(captureId: string): Promise<void> {
	const result = (await api['/v1/captures/{capture_id}'].DELETE({
		params: { path: { capture_id: captureId } }
	})) as Parameters<typeof unwrap>[0];
	unwrap(result, `/v1/captures/${captureId}`);
}

export async function captureToDraft(captureId: string): Promise<VoiceDraft> {
	const result = (await api['/v1/captures/{capture_id}/voice-draft'].POST({
		params: { path: { capture_id: captureId } }
	})) as Parameters<typeof unwrap>[0];
	return parseVoiceDraft(unwrap(result, `/v1/captures/${captureId}/voice-draft`));
}

export type CaptureExportFormat = 'txt' | 'srt' | 'vtt' | 'json';

export async function exportCapture(captureId: string, format: CaptureExportFormat): Promise<Blob> {
	return requestBlob(`/v1/captures/${captureId}/export?format=${format}`);
}

export function captureAudioPath(captureId: string): string {
	return `/v1/captures/${captureId}/audio`;
}

export type TranscribeOptions = {
	file: File;
	model: string;
	language?: string;
	hotwords?: string;
	onProgress?: (ratio: number) => void;
	signal?: AbortSignal;
};

export function transcribeUpload(options: TranscribeOptions): UploadTask<TranscribeResult> {
	const form = new FormData();
	form.set('file', options.file);
	form.set('model', options.model);
	form.set('response_format', 'verbose_json');
	if (options.language && options.language !== 'auto') form.set('language', options.language);
	if (options.hotwords?.trim()) form.set('prompt', options.hotwords.trim());
	return uploadForm('/v1/audio/transcriptions', form, {
		onProgress: options.onProgress,
		parse: parseTranscribeResult,
		...(options.signal ? { signal: options.signal } : {})
	});
}

/** Server-rendered subtitle text for Copy; never re-derived in the browser. */
export async function captureExportText(
	captureId: string,
	format: CaptureExportFormat
): Promise<string> {
	const response = await apiFetch(`/v1/captures/${captureId}/export?format=${format}`, {
		headers: { Accept: '*/*' }
	});
	const body = await readJsonBody(response);
	if (!response.ok) throw new ApiError(parseErrorPayload(body, response.status));
	if (typeof body === 'string') return body;
	if (body === null) return '';
	return JSON.stringify(body, null, 2);
}

export { ApiShapeError };
