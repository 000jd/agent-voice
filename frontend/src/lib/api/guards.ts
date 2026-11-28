/**
 * Response guards.
 *
 * Most v0.7 responses have no OpenAPI response model, so every payload crosses
 * this boundary once. Components only ever see the typed view models from
 * `types.ts`; a malformed body raises `ApiShapeError` instead of spreading
 * `unknown` through the UI.
 */
import type {
	ApiErrorPayload,
	AuthCheck,
	CacheClearResult,
	CacheStats,
	CaptureDetail,
	CapturePage,
	CaptureSummary,
	Diagnostics,
	DesignCandidate,
	DesignResult,
	DraftQuality,
	EngineKind,
	EngineSummary,
	GpuStatus,
	HealthSummary,
	HistoryItem,
	HistoryKind,
	HistoryPage,
	Job,
	JobStarted,
	JobStatus,
	LexiconSnapshot,
	LexiconScope,
	ModelMode,
	ModelSummary,
	PresetVoice,
	QualitySeverity,
	QualityWarning,
	ServerInfo,
	SettingsSnapshot,
	SettingEntry,
	SynthesisResult,
	TranscribeResult,
	TranscribeSegment,
	TranscribeWord,
	UnloadResult,
	VoiceDetail,
	VoiceDraft,
	VoicePreview,
	VoiceSample,
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

function asArray(value: unknown): unknown[] {
	return Array.isArray(value) ? value : [];
}

function requireRecord(value: unknown, endpoint: string): JsonRecord {
	const record = asRecord(value);
	if (!record) throw new ApiShapeError(endpoint, 'expected a JSON object.');
	return record;
}

function stringField(record: JsonRecord, key: string): string | null {
	const value = record[key];
	return typeof value === 'string' ? value : null;
}

function numberField(record: JsonRecord, key: string): number | null {
	const value = record[key];
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function nullableNumber(record: JsonRecord, key: string): number | null {
	return numberField(record, key);
}

function boolField(record: JsonRecord, key: string): boolean {
	return record[key] === true;
}

function stringArray(record: JsonRecord, key: string): string[] {
	return asArray(record[key]).filter((item): item is string => typeof item === 'string');
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

function jsonRecord(value: unknown): Record<string, unknown> | null {
	return asRecord(value);
}

// ---------------------------------------------------------------------------
// Errors, health, GPU
// ---------------------------------------------------------------------------

export function parseErrorPayload(value: unknown, status: number): ApiErrorPayload {
	const record = asRecord(value);
	return {
		status,
		code: record ? (stringField(record, 'error_code') ?? 'HTTP_ERROR') : 'HTTP_ERROR',
		message: record ? (stringField(record, 'message') ?? `HTTP ${status}`) : `HTTP ${status}`,
		suggestion: record ? (stringField(record, 'suggestion') ?? '') : '',
		correlationId: record ? (stringField(record, 'correlation_id') ?? '') : ''
	};
}

export function parseHealth(value: unknown): HealthSummary {
	const endpoint = '/health';
	const record = requireRecord(value, endpoint);
	return {
		status: requireString(record, 'status', endpoint),
		version: requireString(record, 'version', endpoint),
		protocolVersion: requireNumber(record, 'protocol_version', endpoint),
		torchLoaded: boolField(record, 'torch_loaded')
	};
}

export function parseGpu(value: unknown): GpuStatus {
	const record = requireRecord(value, '/v1/gpu');
	return {
		activeEngine: stringField(record, 'active_engine'),
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

// ---------------------------------------------------------------------------
// Engines and models
// ---------------------------------------------------------------------------

function parseModel(value: unknown): ModelSummary {
	const endpoint = '/v1/engines';
	const record = requireRecord(value, endpoint);
	return {
		engineId: stringField(record, 'engine_id') ?? '',
		alias: requireString(record, 'alias', endpoint),
		hfRepo: stringField(record, 'hf_repo') ?? '',
		qualityTier: stringField(record, 'quality_tier') ?? 'unknown',
		estVramGb: numberField(record, 'est_vram_gb') ?? 0,
		isDefault: boolField(record, 'is_default'),
		downloaded: boolField(record, 'downloaded_locally'),
		loaded: boolField(record, 'loaded'),
		mode: literal<ModelMode>(record, 'mode', ['clone', 'custom_voice', 'voice_design'], 'clone'),
		supportsInstruct: boolField(record, 'supports_instruct'),
		presetSpeakers: stringArray(record, 'preset_speakers'),
		kind: literal<EngineKind>(record, 'kind', ['tts', 'asr'], 'tts')
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
		installed: boolField(record, 'installed'),
		kind: literal<EngineKind>(record, 'kind', ['tts', 'asr'], 'tts'),
		device: stringField(record, 'device') ?? 'unknown',
		defaultDevice: stringField(record, 'default_device'),
		supportedDevices: stringArray(record, 'supported_devices'),
		resolvedDevice: stringField(record, 'resolved_device'),
		customVoice: boolField(record, 'custom_voice'),
		voiceDesign: boolField(record, 'voice_design'),
		instructControl: boolField(record, 'instruct_control'),
		wordTimestamps: boolField(record, 'word_timestamps'),
		hotwords: boolField(record, 'hotwords'),
		multilingual: boolField(record, 'multilingual'),
		languages: stringArray(record, 'languages'),
		models: asArray(record.models).map(parseModel),
		whyUnavailable: stringField(record, 'why_unavailable')
	};
}

export function parseEnginesPayload(value: unknown): EngineSummary[] {
	const record = requireRecord(value, '/v1/engines');
	if (!Array.isArray(record.engines)) {
		throw new ApiShapeError('/v1/engines', 'missing engines array.');
	}
	return record.engines.map(parseEngine);
}

export function parseModelsPayload(value: unknown): ModelSummary[] {
	const record = requireRecord(value, '/v1/models');
	if (!Array.isArray(record.models)) {
		throw new ApiShapeError('/v1/models', 'missing models array.');
	}
	return record.models.map(parseModel);
}

// ---------------------------------------------------------------------------
// Voices, presets, previews
// ---------------------------------------------------------------------------

function parseVoiceEngineSupport(value: unknown): VoiceSummary['engineSupport'][number] {
	const record = requireRecord(value, '/v1/voices');
	return {
		engineId: stringField(record, 'engine_id') ?? '',
		modelAlias: stringField(record, 'model_alias') ?? '',
		maxRefSamples: numberField(record, 'max_ref_samples') ?? 1
	};
}

function parseVoiceSummary(value: unknown): VoiceSummary {
	const endpoint = '/v1/voices';
	const record = requireRecord(value, endpoint);
	const source = literal<VoiceSource>(record, 'source', ['cloned', 'designed'], 'cloned');
	return {
		id: requireString(record, 'voice_id', endpoint),
		name: requireString(record, 'name', endpoint),
		kind: literal<VoiceSource>(record, 'kind', ['cloned', 'designed'], source),
		source,
		language: stringField(record, 'language') ?? 'Auto',
		notes: stringField(record, 'notes') ?? '',
		durationSec: numberField(record, 'duration_sec') ?? 0,
		createdAt: numberField(record, 'created_at') ?? 0,
		sampleCount: numberField(record, 'sample_count') ?? 1,
		refText: stringField(record, 'ref_text') ?? '',
		xVectorOnly: boolField(record, 'x_vector_only_mode'),
		warnings: stringArray(record, 'warnings'),
		designInstruct: stringField(record, 'design_instruct') ?? '',
		consentRecordedAt: nullableNumber(record, 'consent_recorded_at'),
		hasPreview: boolField(record, 'has_preview'),
		engineSupport: asArray(record.engine_support).map(parseVoiceEngineSupport)
	};
}

export function parseVoicesPayload(value: unknown): VoiceSummary[] {
	const record = requireRecord(value, '/v1/voices');
	if (!Array.isArray(record.voices)) {
		throw new ApiShapeError('/v1/voices', 'missing voices array.');
	}
	return record.voices.map(parseVoiceSummary);
}

function parseVoiceSample(value: unknown): VoiceSample {
	const record = requireRecord(value, '/v1/voices');
	return {
		id: stringField(record, 'sample_id') ?? '',
		durationSec: numberField(record, 'duration_sec') ?? 0,
		refText: stringField(record, 'ref_text') ?? '',
		isPrimary: boolField(record, 'is_primary'),
		createdAt: numberField(record, 'created_at') ?? 0
	};
}

export function parseVoiceDetail(value: unknown): VoiceDetail {
	const record = requireRecord(value, '/v1/voices/{voice_id}');
	return {
		voice: parseVoiceSummary(record.voice),
		samples: asArray(record.samples).map(parseVoiceSample)
	};
}

export type CreatedVoice = {
	id: string;
	name: string;
	durationSec: number;
	language: string;
	warnings: string[];
	sampleCount: number;
};

/** `POST /v1/voices` and `POST /v1/voices/{id}/samples` return `VoiceMetadata`. */
export function parseCreatedVoice(value: unknown): CreatedVoice {
	const endpoint = '/v1/voices';
	const record = requireRecord(value, endpoint);
	return {
		id: requireString(record, 'voice_id', endpoint),
		name: requireString(record, 'name', endpoint),
		durationSec: numberField(record, 'duration_sec') ?? 0,
		language: stringField(record, 'language') ?? 'Auto',
		warnings: stringArray(record, 'warnings'),
		sampleCount: asArray(record.samples).length
	};
}

function parsePreset(value: unknown): PresetVoice {
	const record = requireRecord(value, '/v1/presets');
	return {
		presetId: requireString(record, 'preset_id', '/v1/presets'),
		engineId: stringField(record, 'engine_id') ?? '',
		modelAlias: stringField(record, 'model_alias') ?? '',
		speaker: stringField(record, 'speaker') ?? '',
		displayName: stringField(record, 'display_name') ?? '',
		languages: stringArray(record, 'languages')
	};
}

export function parsePresetsPayload(value: unknown): PresetVoice[] {
	const record = requireRecord(value, '/v1/presets');
	if (!Array.isArray(record.presets)) {
		throw new ApiShapeError('/v1/presets', 'missing presets array.');
	}
	return record.presets.map(parsePreset);
}

export function parseVoicePreview(value: unknown): VoicePreview {
	const endpoint = '/v1/voices/preview';
	const record = requireRecord(value, endpoint);
	const artifactId = requireString(record, 'artifact_id', endpoint);
	return {
		artifactId,
		downloadUrl: stringField(record, 'download_url') || `/audio/${artifactId}`,
		durationSec: numberField(record, 'duration_sec') ?? 0,
		engineId: stringField(record, 'engine_id') ?? '',
		modelAlias: stringField(record, 'model_alias') ?? '',
		voiceId: stringField(record, 'voice_id'),
		speaker: stringField(record, 'speaker'),
		cached: boolField(record, 'cached')
	};
}

// ---------------------------------------------------------------------------
// Voice drafts
// ---------------------------------------------------------------------------

function parseQualityWarning(value: unknown): QualityWarning {
	const record = requireRecord(value, '/v1/voice-drafts');
	return {
		code: stringField(record, 'code') ?? '',
		severity: literal<QualitySeverity>(record, 'severity', ['info', 'warning', 'error'], 'warning'),
		message: stringField(record, 'message') ?? '',
		suggestion: stringField(record, 'suggestion') ?? ''
	};
}

function parseDraftQuality(value: unknown): DraftQuality {
	const endpoint = '/v1/voice-drafts';
	const record = requireRecord(value, endpoint);
	const clipping = asRecord(record.clipping);
	return {
		durationSec: numberField(record, 'duration_sec') ?? 0,
		peakDbfs: numberField(record, 'peak_dbfs') ?? 0,
		rmsDbfs: numberField(record, 'rms_dbfs') ?? 0,
		truePeakDbtp: numberField(record, 'true_peak_dbtp'),
		integratedLufs: numberField(record, 'integrated_lufs'),
		silenceRatio: numberField(record, 'silence_ratio') ?? 0,
		clipping: {
			detected: clipping ? boolField(clipping, 'detected') : false,
			clippedSamples: clipping ? (numberField(clipping, 'clipped_samples') ?? 0) : 0,
			firstAtSec: clipping ? numberField(clipping, 'first_at_sec') : null
		},
		warnings: asArray(record.warnings).map(parseQualityWarning)
	};
}

export function parseVoiceDraft(value: unknown): VoiceDraft {
	const endpoint = '/v1/voice-drafts';
	const record = requireRecord(value, endpoint);
	const suggestedError = asRecord(record.suggested_transcript_error);
	return {
		draftId: requireString(record, 'draft_id', endpoint),
		createdAt: numberField(record, 'created_at') ?? 0,
		expiresAt: numberField(record, 'expires_at') ?? 0,
		durationSec: numberField(record, 'duration_sec') ?? 0,
		sampleRate: numberField(record, 'sample_rate') ?? 24000,
		channels: numberField(record, 'channels') ?? 1,
		peaks: asArray(record.peaks).filter((item): item is number => typeof item === 'number'),
		quality: parseDraftQuality(record.quality),
		suggestedTranscript: stringField(record, 'suggested_transcript'),
		suggestedTranscriptLanguage: stringField(record, 'suggested_transcript_language'),
		suggestedTranscriptEngine: stringField(record, 'suggested_transcript_engine'),
		suggestedTranscriptError: suggestedError ? parseErrorPayload(suggestedError, 0) : null
	};
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

export function parseHistoryItem(value: unknown): HistoryItem {
	const endpoint = '/v1/history';
	const record = requireRecord(value, endpoint);
	const error = jsonRecord(record.error);
	return {
		id: requireString(record, 'id', endpoint),
		createdAt: numberField(record, 'created_at') ?? 0,
		kind: literal<HistoryKind>(
			record,
			'kind',
			['speak', 'script', 'design', 'transcribe'],
			'speak'
		),
		status: literal<'succeeded' | 'failed'>(record, 'status', ['succeeded', 'failed'], 'succeeded'),
		favorite: boolField(record, 'favorite'),
		text: stringField(record, 'text') ?? '',
		voiceId: stringField(record, 'voice_id'),
		voiceName: stringField(record, 'voice_name') ?? '',
		speaker: stringField(record, 'speaker'),
		engineId: stringField(record, 'engine_id') ?? '',
		modelAlias: stringField(record, 'model_alias') ?? '',
		device: stringField(record, 'device'),
		seed: numberField(record, 'seed'),
		take: numberField(record, 'take'),
		durationSec: numberField(record, 'duration_sec') ?? 0,
		format: stringField(record, 'format') ?? 'wav',
		artifactId: stringField(record, 'artifact_id'),
		audioUrl: stringField(record, 'audio_url') ?? '',
		captureId: stringField(record, 'capture_id'),
		rerenderable: boolField(record, 'rerenderable'),
		error: error ? parseErrorPayload(error, 0) : null
	};
}

export function parseHistoryPage(value: unknown): HistoryPage {
	const record = requireRecord(value, '/v1/history');
	return {
		items: asArray(record.items).map(parseHistoryItem),
		nextCursor: stringField(record, 'next_cursor')
	};
}

// ---------------------------------------------------------------------------
// Captures and transcription
// ---------------------------------------------------------------------------

function parseCaptureSummary(value: unknown): CaptureSummary {
	const endpoint = '/v1/captures';
	const record = requireRecord(value, endpoint);
	return {
		id: requireString(record, 'id', endpoint),
		createdAt: numberField(record, 'created_at') ?? 0,
		title: stringField(record, 'title') ?? '',
		transcript: stringField(record, 'transcript') ?? '',
		language: stringField(record, 'language') ?? '',
		durationSec: numberField(record, 'duration_sec') ?? 0,
		segmentsCount: numberField(record, 'segments_count') ?? 0,
		engineId: stringField(record, 'engine_id') ?? '',
		modelAlias: stringField(record, 'model_alias') ?? '',
		source: literal<'upload' | 'path'>(record, 'source', ['upload', 'path'], 'upload'),
		audioUrl: stringField(record, 'audio_url') ?? '',
		truncated: boolField(record, 'truncated'),
		segmentsOmitted: numberField(record, 'segments_omitted') ?? 0
	};
}

export function parseCapturePage(value: unknown): CapturePage {
	const record = requireRecord(value, '/v1/captures');
	return {
		items: asArray(record.items).map(parseCaptureSummary),
		nextCursor: stringField(record, 'next_cursor')
	};
}

function parseWord(value: unknown): TranscribeWord {
	const record = requireRecord(value, '/v1/captures');
	return {
		word: stringField(record, 'word') ?? '',
		startSec: numberField(record, 'start_sec') ?? 0,
		endSec: numberField(record, 'end_sec') ?? 0
	};
}

function parseSegment(value: unknown): TranscribeSegment {
	const record = requireRecord(value, '/v1/captures');
	return {
		index: numberField(record, 'index') ?? 0,
		startSec: numberField(record, 'start_sec') ?? 0,
		endSec: numberField(record, 'end_sec') ?? 0,
		text: stringField(record, 'text') ?? '',
		words: asArray(record.words).map(parseWord)
	};
}

export function parseCaptureDetail(value: unknown): CaptureDetail {
	const record = requireRecord(value, '/v1/captures/{capture_id}');
	return {
		...parseCaptureSummary(value),
		segments: asArray(record.segments).map(parseSegment),
		fullSegmentsAvailable: boolField(record, 'full_segments_available')
	};
}

export function parseTranscribeResult(value: unknown): TranscribeResult {
	const endpoint = '/v1/audio/transcriptions';
	const record = requireRecord(value, endpoint);
	return {
		text: stringField(record, 'text') ?? '',
		language: stringField(record, 'language') ?? '',
		durationSec: numberField(record, 'duration_sec') ?? numberField(record, 'duration') ?? 0,
		segmentsCount: numberField(record, 'segments_count') ?? 0,
		segments: asArray(record.segments).map(parseSegment),
		engineId: stringField(record, 'engine_id') ?? '',
		modelAlias: stringField(record, 'model_alias') ?? '',
		backendName: stringField(record, 'backend_name') ?? '',
		backendVersion: stringField(record, 'backend_version') ?? '',
		elapsedMs: numberField(record, 'elapsed_ms') ?? 0,
		realtimeMultiple: numberField(record, 'realtime_multiple'),
		truncated: boolField(record, 'truncated'),
		segmentsOmitted: numberField(record, 'segments_omitted') ?? 0,
		transcriptJsonPath: stringField(record, 'transcript_json_path'),
		srtPath: stringField(record, 'srt_path'),
		vttPath: stringField(record, 'vtt_path'),
		captureId: stringField(record, 'capture_id')
	};
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export function parseJob(value: unknown): Job {
	const endpoint = '/v1/jobs';
	const record = requireRecord(value, endpoint);
	const error = jsonRecord(record.error);
	return {
		jobId: requireString(record, 'job_id', endpoint),
		kind: stringField(record, 'kind') ?? '',
		status: literal<JobStatus>(
			record,
			'status',
			['pending', 'running', 'succeeded', 'failed', 'cancelled'],
			'pending'
		),
		progress: numberField(record, 'progress') ?? 0,
		message: stringField(record, 'message') ?? '',
		createdAt: numberField(record, 'created_at') ?? 0,
		updatedAt: numberField(record, 'updated_at') ?? 0,
		result: jsonRecord(record.result),
		error: error ? parseErrorPayload(error, 0) : null
	};
}

export function parseJobsPayload(value: unknown): Job[] {
	const record = requireRecord(value, '/v1/jobs');
	return asArray(record.jobs).map(parseJob);
}

export function parseJobStarted(value: unknown): JobStarted {
	const endpoint = '/v1/jobs';
	const record = requireRecord(value, endpoint);
	return {
		jobId: requireString(record, 'job_id', endpoint),
		kind: stringField(record, 'kind') ?? ''
	};
}

// ---------------------------------------------------------------------------
// Synthesis and design
// ---------------------------------------------------------------------------

export function parseSynthesisResult(value: unknown): SynthesisResult {
	const endpoint = '/v1/agent/speak';
	const record = requireRecord(value, endpoint);
	const artifactId = requireString(record, 'artifact_id', endpoint);
	return {
		artifactId,
		filePath: stringField(record, 'file_path') ?? '',
		durationSec: requireNumber(record, 'duration_sec', endpoint),
		sampleRate: numberField(record, 'sample_rate') ?? 24000,
		format: stringField(record, 'format') ?? 'wav',
		engineId: stringField(record, 'engine_id') ?? '',
		modelAlias: stringField(record, 'model_alias') ?? '',
		backendName: stringField(record, 'backend_name') ?? '',
		backendVersion: stringField(record, 'backend_version') ?? '',
		voiceId: stringField(record, 'voice_id'),
		mode: literal<ModelMode>(record, 'mode', ['clone', 'custom_voice', 'voice_design'], 'clone'),
		speaker: stringField(record, 'speaker'),
		cached: boolField(record, 'cached'),
		downloadUrl: stringField(record, 'download_url') || `/audio/${artifactId}`,
		chunksUsed: numberField(record, 'chunks_used') ?? 1,
		elapsedMs: numberField(record, 'elapsed_ms') ?? 0,
		take: numberField(record, 'take'),
		realtimeMultiple:
			numberField(record, 'realtime_multiple') ?? numberField(record, 'real_time_factor')
	};
}

function parseDesignCandidate(value: unknown): DesignCandidate {
	const record = requireRecord(value, '/v1/voices/design');
	return {
		take: numberField(record, 'take') ?? 1,
		seed: numberField(record, 'seed'),
		filePath: stringField(record, 'file_path') ?? '',
		downloadUrl: stringField(record, 'download_url') ?? '',
		durationSec: numberField(record, 'duration_sec') ?? 0
	};
}

export function parseDesignResult(value: unknown): DesignResult {
	const endpoint = '/v1/voices/design';
	const record = requireRecord(value, endpoint);
	return {
		voiceId: stringField(record, 'voice_id'),
		name: requireString(record, 'name', endpoint),
		previewFilePath: stringField(record, 'preview_file_path') ?? '',
		durationSec: numberField(record, 'duration_sec') ?? 0,
		source: 'designed',
		designInstruct: stringField(record, 'design_instruct') ?? '',
		engineId: stringField(record, 'engine_id') ?? '',
		modelAlias: stringField(record, 'model_alias') ?? '',
		candidates: asArray(record.candidates).map(parseDesignCandidate)
	};
}

// ---------------------------------------------------------------------------
// Settings, diagnostics, lexicon, cache, auth, server info
// ---------------------------------------------------------------------------

function parseSettingEntry(value: unknown): SettingEntry {
	const record = requireRecord(value, '/v1/settings');
	return {
		key: requireString(record, 'key', '/v1/settings'),
		value: record.value,
		kind: literal<SettingEntry['kind']>(
			record,
			'kind',
			['bool', 'int', 'float', 'str', 'select', 'readonly'],
			'str'
		),
		group: stringField(record, 'group') ?? 'General',
		label: stringField(record, 'label') ?? '',
		description: stringField(record, 'description') ?? '',
		editable: boolField(record, 'editable'),
		restartRequired: boolField(record, 'restart_required'),
		choices: stringArray(record, 'choices'),
		source: literal<SettingEntry['source']>(
			record,
			'source',
			['env', 'file', 'default', 'readonly'],
			'default'
		),
		shadowedByEnv: boolField(record, 'shadowed_by_env')
	};
}

export function parseSettingsSnapshot(value: unknown): SettingsSnapshot {
	const endpoint = '/v1/settings';
	const record = requireRecord(value, endpoint);
	const values = asRecord(record.values);
	return {
		settings: asArray(record.settings).map(parseSettingEntry),
		values: values ?? {},
		configPath: stringField(record, 'config_path') ?? '',
		restartRequired: boolField(record, 'restart_required')
	};
}

export function parseDiagnostics(value: unknown): Diagnostics {
	const record = requireRecord(value, '/v1/diagnostics');
	return {
		checkedAt: numberField(record, 'checked_at') ?? 0,
		checks: asArray(record.checks).map((item) => {
			const check = requireRecord(item, '/v1/diagnostics');
			return {
				name: stringField(check, 'name') ?? '',
				status: literal<DiagnosticCheckStatus>(
					check,
					'status',
					['PASS', 'INFO', 'WARN', 'FAIL'],
					'INFO'
				),
				detail: stringField(check, 'detail') ?? '',
				fix: stringField(check, 'fix') ?? ''
			};
		})
	};
}

type DiagnosticCheckStatus = 'PASS' | 'INFO' | 'WARN' | 'FAIL';

function parseLexiconScope(value: unknown): LexiconScope {
	const record = requireRecord(value, '/v1/lexicon');
	const entries = asRecord(record.entries) ?? {};
	const parsed: Record<string, string> = {};
	for (const [word, replacement] of Object.entries(entries)) {
		if (typeof replacement === 'string') parsed[word] = replacement;
	}
	return {
		scope: stringField(record, 'scope') ?? '',
		path: stringField(record, 'path') ?? '',
		entries: parsed
	};
}

export function parseLexiconSnapshot(value: unknown): LexiconSnapshot {
	const record = requireRecord(value, '/v1/lexicon');
	const effective = asRecord(record.effective) ?? {};
	const merged: Record<string, string> = {};
	for (const [word, replacement] of Object.entries(effective)) {
		if (typeof replacement === 'string') merged[word] = replacement;
	}
	return {
		project: stringField(record, 'project'),
		voiceId: stringField(record, 'voice_id'),
		scopes: asArray(record.scopes).map(parseLexiconScope),
		mergedEntries: numberField(record, 'merged_entries') ?? 0,
		effective: merged,
		pronunciationHash: stringField(record, 'pronunciation_hash') ?? ''
	};
}

export function parseCacheStats(value: unknown): CacheStats {
	const record = requireRecord(value, '/v1/cache');
	return {
		cacheDir: stringField(record, 'cache_dir') ?? '',
		entries: numberField(record, 'entries') ?? 0,
		totalBytes: numberField(record, 'total_bytes') ?? 0,
		maxBytes: numberField(record, 'max_bytes') ?? 0
	};
}

export function parseCacheClearResult(value: unknown): CacheClearResult {
	const record = requireRecord(value, '/v1/cache');
	return {
		filesDeleted: numberField(record, 'files_deleted') ?? 0,
		bytesFreed: numberField(record, 'bytes_freed') ?? 0
	};
}

export function parseAuthCheck(value: unknown): AuthCheck {
	const record = requireRecord(value, '/v1/auth/check');
	return {
		authenticated: boolField(record, 'authenticated'),
		authRequired: boolField(record, 'auth_required')
	};
}

export function parseServerInfo(value: unknown): ServerInfo {
	const endpoint = '/v1/server_info';
	const record = requireRecord(value, endpoint);
	return {
		version: stringField(record, 'version') ?? '',
		protocolVersion: numberField(record, 'protocol_version') ?? 0,
		dataDir: stringField(record, 'data_dir') ?? '',
		home: stringField(record, 'home') ?? '',
		inboxDir: stringField(record, 'inbox_dir') ?? '',
		artifactsDir: stringField(record, 'artifacts_dir') ?? '',
		enginesDir: stringField(record, 'engines_dir') ?? '',
		defaultEngine: stringField(record, 'default_engine') ?? '',
		defaultAsrEngine: stringField(record, 'default_asr_engine') ?? '',
		defaultVoiceId: stringField(record, 'default_voice_id') ?? '',
		voicesCount: numberField(record, 'voices_count') ?? 0,
		historyCount: numberField(record, 'history_count') ?? 0,
		capturesCount: numberField(record, 'captures_count') ?? 0,
		nextSteps: stringArray(record, 'next_steps')
	};
}

export function parseUnloadResult(value: unknown): UnloadResult {
	const record = requireRecord(value, '/v1/models/unload');
	return {
		unloaded: stringArray(record, 'unloaded'),
		cancelledJobs: numberField(record, 'cancelled_jobs') ?? 0
	};
}

/** `/v1/mcp-config` is a client-specific JSON blob; render it verbatim. */
export function parseMcpConfig(value: unknown): Record<string, unknown> {
	const record = asRecord(value);
	if (!record) throw new ApiShapeError('/v1/mcp-config', 'expected a JSON object.');
	return record;
}
