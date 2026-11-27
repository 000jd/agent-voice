import type {
	ApiErrorPayload,
	DesignCandidate,
	DesignResult,
	EngineKind,
	EngineSummary,
	GpuStatus,
	HealthSummary,
	ModelMode,
	ModelSummary,
	SpeakResult,
	TranscribeResult,
	TranscribeSegment,
	TranscribeWord,
	UnloadResult,
	VoiceSource,
	VoiceSummary
} from './types.js';

type JsonRecord = Record<string, unknown>;

export class ApiShapeError extends Error {
	constructor(endpoint: string, detail: string) {
		super(`Malformed response from ${endpoint}: ${detail}`);
		this.name = 'ApiShapeError';
	}
}

function asRecord(value: unknown): JsonRecord | null {
	return typeof value === 'object' && value !== null && !Array.isArray(value)
		? (value as JsonRecord)
		: null;
}

function stringField(record: JsonRecord, key: string): string | null {
	const value = record[key];
	return typeof value === 'string' ? value : null;
}

function numberField(record: JsonRecord, key: string): number | null {
	const value = record[key];
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function booleanField(record: JsonRecord, key: string): boolean {
	return record[key] === true;
}

function nullableString(record: JsonRecord, key: string): string | null {
	return stringField(record, key);
}

function stringArray(record: JsonRecord, key: string): string[] {
	const value = record[key];
	return Array.isArray(value)
		? value.filter((item): item is string => typeof item === 'string')
		: [];
}

function recordOfStrings(record: JsonRecord, key: string): Record<string, string> {
	const value = asRecord(record[key]);
	if (!value) return {};
	const result: Record<string, string> = {};
	for (const [name, item] of Object.entries(value)) {
		if (typeof item === 'string') result[name] = item;
	}
	return result;
}

function recordOfNumbers(record: JsonRecord, key: string): Record<string, number | null> {
	const value = asRecord(record[key]);
	if (!value) return {};
	const result: Record<string, number | null> = {};
	for (const [name, item] of Object.entries(value)) {
		result[name] = typeof item === 'number' && Number.isFinite(item) ? item : null;
	}
	return result;
}

function requireRecord(value: unknown, endpoint: string): JsonRecord {
	const record = asRecord(value);
	if (!record) throw new ApiShapeError(endpoint, 'expected a JSON object.');
	return record;
}

function requireString(record: JsonRecord, key: string, endpoint: string): string {
	const value = stringField(record, key);
	if (value === null) throw new ApiShapeError(endpoint, `missing string field ${key}.`);
	return value;
}

function requireNumber(record: JsonRecord, key: string, endpoint: string): number {
	const value = numberField(record, key);
	if (value === null) throw new ApiShapeError(endpoint, `missing numeric field ${key}.`);
	return value;
}

function literal<T extends string>(
	record: JsonRecord,
	key: string,
	choices: readonly T[],
	fallback: T
): T {
	const value = record[key];
	return typeof value === 'string' && choices.includes(value as T) ? (value as T) : fallback;
}

export function parseHealth(value: unknown): HealthSummary {
	const endpoint = '/health';
	const record = requireRecord(value, endpoint);
	return {
		status: requireString(record, 'status', endpoint),
		version: requireString(record, 'version', endpoint),
		protocolVersion: requireNumber(record, 'protocol_version', endpoint),
		torchLoaded: booleanField(record, 'torch_loaded')
	};
}

export function parseGpu(value: unknown): GpuStatus {
	const endpoint = '/v1/gpu';
	const record = requireRecord(value, endpoint);
	return {
		activeEngine: nullableString(record, 'active_engine'),
		loadedModels: stringArray(record, 'loaded_models'),
		workers: recordOfStrings(record, 'workers'),
		cpuEngines: stringArray(record, 'cpu_engines'),
		workerDevices: recordOfStrings(record, 'worker_devices'),
		workerVramMb: recordOfNumbers(record, 'worker_vram_mb'),
		vramSource: stringField(record, 'vram_source') ?? 'none',
		vramUsedMb: numberField(record, 'vram_used_mb'),
		vramFreeMb: numberField(record, 'vram_free_mb'),
		queueDepth: numberField(record, 'queue_depth') ?? 0,
		inFlight: numberField(record, 'in_flight') ?? 0,
		unloadInSeconds: numberField(record, 'unload_in_seconds'),
		lastUsedAt: numberField(record, 'last_used_at')
	};
}

function parseModel(value: unknown): ModelSummary {
	const endpoint = '/v1/engines';
	const record = requireRecord(value, endpoint);
	const kind = literal<EngineKind>(record, 'kind', ['tts', 'asr'], 'tts');
	return {
		engineId: stringField(record, 'engine_id') ?? '',
		alias: requireString(record, 'alias', endpoint),
		hfRepo: stringField(record, 'hf_repo') ?? '',
		qualityTier: stringField(record, 'quality_tier') ?? 'unknown',
		estVramGb: numberField(record, 'est_vram_gb') ?? 0,
		isDefault: booleanField(record, 'is_default'),
		downloaded: booleanField(record, 'downloaded_locally'),
		loaded: booleanField(record, 'loaded'),
		mode: literal<ModelMode>(record, 'mode', ['clone', 'custom_voice', 'voice_design'], 'clone'),
		supportsInstruct: booleanField(record, 'supports_instruct'),
		presetSpeakers: stringArray(record, 'preset_speakers'),
		kind
	};
}

function parseEngine(value: unknown): EngineSummary {
	const endpoint = '/v1/engines';
	const record = requireRecord(value, endpoint);
	const id = requireString(record, 'engine_id', endpoint);
	return {
		id,
		displayName: stringField(record, 'display_name') ?? id,
		backend: stringField(record, 'backend_name') ?? 'unknown',
		backendVersion: stringField(record, 'backend_version') ?? '',
		installed: booleanField(record, 'installed'),
		kind: literal<EngineKind>(record, 'kind', ['tts', 'asr'], 'tts'),
		device: stringField(record, 'device') ?? 'unknown',
		defaultDevice: nullableString(record, 'default_device'),
		supportedDevices: stringArray(record, 'supported_devices'),
		resolvedDevice: nullableString(record, 'resolved_device'),
		customVoice: booleanField(record, 'custom_voice'),
		voiceDesign: booleanField(record, 'voice_design'),
		instructControl: booleanField(record, 'instruct_control'),
		wordTimestamps: booleanField(record, 'word_timestamps'),
		hotwords: booleanField(record, 'hotwords'),
		multilingual: booleanField(record, 'multilingual'),
		languages: stringArray(record, 'languages'),
		models: Array.isArray(record.models) ? record.models.map(parseModel) : [],
		whyUnavailable: nullableString(record, 'why_unavailable')
	};
}

export function parseEnginesPayload(value: unknown): EngineSummary[] {
	const record = requireRecord(value, '/v1/engines');
	if (!Array.isArray(record.engines)) {
		throw new ApiShapeError('/v1/engines', 'missing engines array.');
	}
	return record.engines.map(parseEngine);
}

function parseVoice(value: unknown): VoiceSummary {
	const endpoint = '/v1/voices';
	const record = requireRecord(value, endpoint);
	return {
		id: requireString(record, 'voice_id', endpoint),
		name: requireString(record, 'name', endpoint),
		refText: stringField(record, 'ref_text') ?? '',
		xVectorOnly: booleanField(record, 'x_vector_only_mode'),
		durationSec: numberField(record, 'duration_sec') ?? 0,
		createdAt: numberField(record, 'created_at') ?? 0,
		warnings: stringArray(record, 'warnings'),
		source: literal<VoiceSource>(record, 'source', ['cloned', 'designed'], 'cloned'),
		designInstruct: stringField(record, 'design_instruct') ?? ''
	};
}

export function parseVoicesPayload(value: unknown): VoiceSummary[] {
	const record = requireRecord(value, '/v1/voices');
	if (!Array.isArray(record.voices)) {
		throw new ApiShapeError('/v1/voices', 'missing voices array.');
	}
	return record.voices.map(parseVoice);
}

export function parseSpeakResult(value: unknown): SpeakResult {
	const endpoint = '/v1/agent/speak';
	const record = requireRecord(value, endpoint);
	const artifactId = requireString(record, 'artifact_id', endpoint);
	return {
		artifactId,
		filePath: requireString(record, 'file_path', endpoint),
		durationSec: requireNumber(record, 'duration_sec', endpoint),
		sampleRate: numberField(record, 'sample_rate') ?? 24000,
		format: stringField(record, 'format') ?? 'wav',
		engineId: requireString(record, 'engine_id', endpoint),
		modelAlias: requireString(record, 'model_alias', endpoint),
		backendName: stringField(record, 'backend_name') ?? '',
		backendVersion: stringField(record, 'backend_version') ?? '',
		voiceId: nullableString(record, 'voice_id'),
		mode: literal<ModelMode>(record, 'mode', ['clone', 'custom_voice', 'voice_design'], 'clone'),
		speaker: nullableString(record, 'speaker'),
		cached: booleanField(record, 'cached'),
		streamingMode: nullableString(record, 'streaming_mode'),
		downloadUrl: stringField(record, 'download_url') || `/audio/${artifactId}`,
		chunksUsed: numberField(record, 'chunks_used') ?? 1,
		elapsedMs: numberField(record, 'elapsed_ms') ?? 0,
		take: numberField(record, 'take'),
		realtimeMultiple:
			numberField(record, 'realtime_multiple') ?? numberField(record, 'real_time_factor')
	};
}

function parseDesignCandidate(value: unknown): DesignCandidate {
	const endpoint = '/v1/voices/design';
	const record = requireRecord(value, endpoint);
	return {
		take: requireNumber(record, 'take', endpoint),
		seed: numberField(record, 'seed'),
		filePath: requireString(record, 'file_path', endpoint),
		downloadUrl: stringField(record, 'download_url') ?? '',
		durationSec: requireNumber(record, 'duration_sec', endpoint)
	};
}

export function parseDesignResult(value: unknown): DesignResult {
	const endpoint = '/v1/voices/design';
	const record = requireRecord(value, endpoint);
	return {
		voiceId: nullableString(record, 'voice_id'),
		name: requireString(record, 'name', endpoint),
		previewFilePath: requireString(record, 'preview_file_path', endpoint),
		durationSec: requireNumber(record, 'duration_sec', endpoint),
		source: 'designed',
		designInstruct: requireString(record, 'design_instruct', endpoint),
		engineId: requireString(record, 'engine_id', endpoint),
		modelAlias: requireString(record, 'model_alias', endpoint),
		candidates: Array.isArray(record.candidates) ? record.candidates.map(parseDesignCandidate) : []
	};
}

function parseWord(value: unknown): TranscribeWord {
	const record = requireRecord(value, '/v1/audio/transcriptions');
	return {
		word: stringField(record, 'word') ?? '',
		startSec: numberField(record, 'start_sec') ?? 0,
		endSec: numberField(record, 'end_sec') ?? 0
	};
}

function parseSegment(value: unknown): TranscribeSegment {
	const record = requireRecord(value, '/v1/audio/transcriptions');
	return {
		index: numberField(record, 'index') ?? 0,
		startSec: numberField(record, 'start_sec') ?? 0,
		endSec: numberField(record, 'end_sec') ?? 0,
		text: stringField(record, 'text') ?? '',
		words: Array.isArray(record.words) ? record.words.map(parseWord) : []
	};
}

export function parseTranscribeResult(value: unknown): TranscribeResult {
	const endpoint = '/v1/audio/transcriptions';
	const record = requireRecord(value, endpoint);
	return {
		text: requireString(record, 'text', endpoint),
		language: stringField(record, 'language') ?? '',
		durationSec: numberField(record, 'duration_sec') ?? 0,
		segmentsCount: numberField(record, 'segments_count') ?? 0,
		segments: Array.isArray(record.segments) ? record.segments.map(parseSegment) : [],
		engineId: stringField(record, 'engine_id') ?? '',
		modelAlias: stringField(record, 'model_alias') ?? '',
		backendName: stringField(record, 'backend_name') ?? '',
		backendVersion: stringField(record, 'backend_version') ?? '',
		elapsedMs: numberField(record, 'elapsed_ms') ?? 0,
		realtimeMultiple: numberField(record, 'realtime_multiple'),
		truncated: booleanField(record, 'truncated'),
		segmentsOmitted: numberField(record, 'segments_omitted') ?? 0,
		transcriptJsonPath: nullableString(record, 'transcript_json_path'),
		srtPath: nullableString(record, 'srt_path'),
		vttPath: nullableString(record, 'vtt_path')
	};
}

export function parseUnloadResult(value: unknown): UnloadResult {
	const endpoint = '/v1/models/unload';
	const record = requireRecord(value, endpoint);
	return {
		unloaded: stringArray(record, 'unloaded'),
		cancelledJobs: numberField(record, 'cancelled_jobs') ?? 0
	};
}

export function parseErrorPayload(value: unknown, status: number): ApiErrorPayload {
	const record = asRecord(value);
	return {
		status,
		code: record ? (stringField(record, 'error_code') ?? 'HTTP_ERROR') : 'HTTP_ERROR',
		message: record ? (stringField(record, 'message') ?? `HTTP ${status}`) : `HTTP ${status}`,
		suggestion: record ? (stringField(record, 'suggestion') ?? '') : ''
	};
}
