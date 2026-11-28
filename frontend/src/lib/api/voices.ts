/** Voice library: list, detail, rename, delete, previews, samples, export/import. */
import { api, apiFetch, readJsonBody, requestBlob, unwrap } from './client.js';
import {
	ApiShapeError,
	parseCreatedVoice,
	parseErrorPayload,
	parseVoiceDetail,
	parseVoicePreview,
	parseVoicesPayload,
	type CreatedVoice
} from './guards.js';
import { ApiError } from './client.js';
import type { VoiceDetail, VoicePreview, VoiceSummary } from './types.js';
import type { components } from './schema.js';

type WireTrim = components['schemas']['DraftTrim'];

export type VoicePatch = {
	name?: string;
	language?: string;
	notes?: string;
};

export type CloneFromDraftInput = {
	name: string;
	draftId: string;
	trim: { startSec: number; endSec: number } | null;
	refText: string;
	xVectorOnly: boolean;
	language: string;
	notes: string;
	consent: boolean;
};

export type AddSampleInput = {
	voiceId: string;
	draftId: string;
	trim: { startSec: number; endSec: number } | null;
	refText: string;
	xVectorOnly: boolean;
	makePrimary: boolean;
	consent: boolean;
};

export function wireTrim(trim: { startSec: number; endSec: number } | null): WireTrim | null {
	if (!trim) return null;
	return { start_sec: Math.max(0, trim.startSec), end_sec: Math.max(0, trim.endSec) };
}

export async function listVoices(): Promise<VoiceSummary[]> {
	const result = (await api['/v1/voices'].GET()) as Parameters<typeof unwrap>[0];
	return parseVoicesPayload(unwrap(result, '/v1/voices'));
}

export async function getVoice(voiceId: string): Promise<VoiceDetail> {
	const result = (await api['/v1/voices/{voice_id}'].GET({
		params: { path: { voice_id: voiceId } }
	})) as Parameters<typeof unwrap>[0];
	return parseVoiceDetail(unwrap(result, `/v1/voices/${voiceId}`));
}

export async function patchVoice(voiceId: string, patch: VoicePatch): Promise<CreatedVoice> {
	const result = (await api['/v1/voices/{voice_id}'].PATCH({
		params: { path: { voice_id: voiceId } },
		body: {
			name: patch.name ?? null,
			language: patch.language ?? null,
			notes: patch.notes ?? null
		}
	})) as Parameters<typeof unwrap>[0];
	return parseCreatedVoice(unwrap(result, `/v1/voices/${voiceId}`));
}

export async function deleteVoice(voiceId: string): Promise<void> {
	const result = (await api['/v1/voices/{voice_id}'].DELETE({
		params: { path: { voice_id: voiceId } }
	})) as Parameters<typeof unwrap>[0];
	unwrap(result, `/v1/voices/${voiceId}`);
}

export async function createVoiceFromDraft(input: CloneFromDraftInput): Promise<CreatedVoice> {
	const result = (await api['/v1/voices'].POST({
		body: {
			name: input.name,
			draft_id: input.draftId,
			trim: wireTrim(input.trim),
			ref_text: input.refText,
			x_vector_only: input.xVectorOnly,
			language: input.language,
			notes: input.notes,
			consent: input.consent
		}
	})) as Parameters<typeof unwrap>[0];
	return parseCreatedVoice(unwrap(result, '/v1/voices'));
}

export async function addVoiceSample(input: AddSampleInput): Promise<CreatedVoice> {
	const result = (await api['/v1/voices/{voice_id}/samples'].POST({
		params: { path: { voice_id: input.voiceId } },
		body: {
			draft_id: input.draftId,
			trim: wireTrim(input.trim),
			ref_text: input.refText,
			x_vector_only: input.xVectorOnly,
			make_primary: input.makePrimary,
			consent: input.consent
		}
	})) as Parameters<typeof unwrap>[0];
	return parseCreatedVoice(unwrap(result, `/v1/voices/${input.voiceId}/samples`));
}

export async function previewVoice(
	voiceId: string,
	text: string,
	engine?: string | null,
	model?: string | null
): Promise<VoicePreview> {
	const result = (await api['/v1/voices/{voice_id}/preview'].POST({
		params: { path: { voice_id: voiceId } },
		body: { text, engine: engine ?? null, model: model ?? null }
	})) as Parameters<typeof unwrap>[0];
	return parseVoicePreview(unwrap(result, `/v1/voices/${voiceId}/preview`));
}

export function voiceExportPath(voiceId: string): string {
	return `/v1/voices/${voiceId}/export`;
}

export async function exportVoice(voiceId: string): Promise<Blob> {
	return requestBlob(voiceExportPath(voiceId));
}

export async function importVoice(file: File, overwrite = false): Promise<CreatedVoice> {
	const form = new FormData();
	form.set('file', file);
	const response = await apiFetch(`/v1/voices/import?overwrite=${overwrite ? 'true' : 'false'}`, {
		method: 'POST',
		body: form
	});
	const body = await readJsonBody(response);
	if (!response.ok) throw new ApiError(parseErrorPayload(body, response.status));
	try {
		return parseCreatedVoice(body);
	} catch (error) {
		if (error instanceof ApiShapeError) throw error;
		throw new ApiShapeError('/v1/voices/import', 'could not parse the response body.');
	}
}
