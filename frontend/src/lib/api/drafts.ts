/** Voice drafts: upload, inspect, discard. A draft is abandoned if not saved. */
import { api, unwrap } from './client.js';
import { parseVoiceDraft } from './guards.js';
import type { VoiceDraft } from './types.js';
import { uploadForm, type UploadTask } from './uploads.js';

export function uploadDraft(
	file: File,
	options: { language?: string; onProgress?: (ratio: number) => void; signal?: AbortSignal }
): UploadTask<VoiceDraft> {
	const form = new FormData();
	form.set('file', file);
	if (options.language && options.language !== 'Auto') form.set('language', options.language);
	return uploadForm('/v1/voice-drafts', form, {
		onProgress: options.onProgress,
		parse: parseVoiceDraft,
		...(options.signal ? { signal: options.signal } : {})
	});
}

export async function getDraft(draftId: string): Promise<VoiceDraft> {
	const result = await api['/v1/voice-drafts/{draft_id}'].GET({
		params: { path: { draft_id: draftId } }
	});
	return parseVoiceDraft(unwrap(result, `/v1/voice-drafts/${draftId}`));
}

/** Best-effort discard: a draft that already expired (410) is not an error. */
export async function deleteDraft(draftId: string): Promise<void> {
	try {
		const result = await api['/v1/voice-drafts/{draft_id}'].DELETE({
			params: { path: { draft_id: draftId } }
		});
		unwrap(result, `/v1/voice-drafts/${draftId}`);
	} catch (error) {
		const status =
			typeof error === 'object' && error !== null && 'status' in error ? error.status : 0;
		if (status === 404 || status === 410) return;
		throw error;
	}
}

export function draftAudioPath(draftId: string): string {
	return `/v1/voice-drafts/${draftId}/audio`;
}
