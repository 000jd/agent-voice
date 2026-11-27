import { api } from './client.js';
import {
	ApiShapeError,
	parseDesignResult,
	parseEnginesPayload,
	parseErrorPayload,
	parseGpu,
	parseHealth,
	parseSpeakResult,
	parseTranscribeResult,
	parseUnloadResult,
	parseVoicesPayload
} from './guards.js';
import type {
	ApiErrorPayload,
	CloneVoiceRequest,
	DesignResult,
	DesignVoiceRequest,
	EngineSummary,
	GpuStatus,
	HealthSummary,
	SpeakRequest,
	SpeakResult,
	TranscribeResult,
	UnloadResult,
	VoiceSummary
} from './types.js';

type RawResult = {
	data?: unknown;
	error?: unknown;
	response: Response;
};

export class ApiError extends Error {
	readonly status: number;
	readonly code: string;
	readonly suggestion: string;

	constructor(payload: ApiErrorPayload) {
		super(payload.message);
		this.name = 'ApiError';
		this.status = payload.status;
		this.code = payload.code;
		this.suggestion = payload.suggestion;
	}
}

function unwrap(result: RawResult, endpoint: string): unknown {
	if (result.error || result.data === undefined) {
		throw new ApiError(parseErrorPayload(result.error, result.response.status));
	}
	try {
		return result.data;
	} catch (error) {
		if (error instanceof ApiShapeError) throw error;
		throw new ApiShapeError(endpoint, 'could not read the response body.');
	}
}

function errorMessage(error: unknown): string {
	if (error instanceof ApiError) {
		return error.suggestion ? `${error.message} ${error.suggestion}` : error.message;
	}
	if (error instanceof Error) return error.message;
	return 'The daemon returned an unknown error.';
}

export function describeApiError(error: unknown): ApiErrorPayload {
	if (error instanceof ApiError) {
		return {
			status: error.status,
			code: error.code,
			message: error.message,
			suggestion: error.suggestion
		};
	}
	return {
		status: 0,
		code: 'CLIENT_ERROR',
		message: errorMessage(error),
		suggestion: ''
	};
}

async function parseJsonResponse(response: Response): Promise<unknown> {
	const text = await response.text();
	if (!text) return null;
	try {
		return JSON.parse(text) as unknown;
	} catch {
		return text;
	}
}

export async function getHealth(): Promise<HealthSummary> {
	const result = (await api['/health'].GET()) as RawResult;
	return parseHealth(unwrap(result, '/health'));
}

export async function getGpu(): Promise<GpuStatus> {
	const result = (await api['/v1/gpu'].GET()) as RawResult;
	return parseGpu(unwrap(result, '/v1/gpu'));
}

export async function getEngines(): Promise<EngineSummary[]> {
	const result = (await api['/v1/engines'].GET()) as RawResult;
	return parseEnginesPayload(unwrap(result, '/v1/engines'));
}

export async function getVoices(): Promise<VoiceSummary[]> {
	const result = (await api['/v1/voices'].GET()) as RawResult;
	return parseVoicesPayload(unwrap(result, '/v1/voices'));
}

export async function speak(request: SpeakRequest): Promise<SpeakResult> {
	const result = (await api['/v1/agent/speak'].POST({ body: request })) as RawResult;
	return parseSpeakResult(unwrap(result, '/v1/agent/speak'));
}

export async function designVoice(request: DesignVoiceRequest): Promise<DesignResult> {
	const result = (await api['/v1/voices/design'].POST({ body: request })) as RawResult;
	return parseDesignResult(unwrap(result, '/v1/voices/design'));
}

export async function cloneVoice(request: CloneVoiceRequest): Promise<VoiceSummary> {
	const result = (await api['/v1/voices'].POST({ body: request })) as RawResult;
	const payload = unwrap(result, '/v1/voices');
	if (typeof payload !== 'object' || payload === null) {
		throw new ApiShapeError('/v1/voices', 'expected a voice object.');
	}
	const voices = parseVoicesPayload({ voices: [payload] });
	const voice = voices[0];
	if (!voice) throw new ApiShapeError('/v1/voices', 'missing created voice.');
	return voice;
}

export async function deleteVoice(voiceId: string): Promise<void> {
	const result = (await api['/v1/voices/{voice_id}'].DELETE({
		params: { path: { voice_id: voiceId } }
	})) as RawResult;
	unwrap(result, `/v1/voices/${voiceId}`);
}

export async function unloadModels(): Promise<UnloadResult> {
	const result = (await api['/v1/models/unload'].POST()) as RawResult;
	return parseUnloadResult(unwrap(result, '/v1/models/unload'));
}

export async function transcribeFile(
	file: File,
	options: { model: string; language?: string; hotwords?: string }
): Promise<TranscribeResult> {
	const form = new FormData();
	form.set('file', file);
	form.set('model', options.model);
	form.set('response_format', 'verbose_json');
	if (options.language && options.language !== 'auto') form.set('language', options.language);
	if (options.hotwords?.trim()) form.set('prompt', options.hotwords.trim());

	const response = await fetch('/v1/audio/transcriptions', {
		method: 'POST',
		body: form,
		credentials: 'same-origin',
		headers: { Accept: 'application/json' }
	});
	if (!response.ok) {
		throw new ApiError(parseErrorPayload(await parseJsonResponse(response), response.status));
	}
	return parseTranscribeResult(await response.json());
}

export async function fetchAudioBlob(url: string): Promise<Blob> {
	const response = await fetch(url, { credentials: 'same-origin' });
	if (!response.ok) {
		const payload = parseErrorPayload(await parseJsonResponse(response), response.status);
		throw new ApiError(payload);
	}
	return response.blob();
}
