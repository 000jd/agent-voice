/**
 * View types for the Studio UI.
 *
 * Every field here mirrors a documented v0.7 REST payload (see
 * `src/agent_voice/schemas.py`). Parsers in `guards.ts` are the only place that
 * may turn `unknown` into one of these; components never see raw JSON.
 */

export type EngineKind = 'tts' | 'asr';
export type ModelMode = 'clone' | 'custom_voice' | 'voice_design';
export type AudioFormat = 'wav' | 'mp3' | 'opus' | 'flac';
export type VoiceSource = 'cloned' | 'designed';
export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled';
export type HealthState = 'checking' | 'connected' | 'offline';

export type ApiErrorPayload = {
	status: number;
	code: string;
	message: string;
	suggestion: string;
	correlationId: string;
};

export type HealthSummary = {
	status: string;
	version: string;
	protocolVersion: number;
	torchLoaded: boolean;
};

export type GpuStatus = {
	activeEngine: string | null;
	loadedModels: string[];
	workers: Record<string, string>;
	cpuEngines: string[];
	workerDevices: Record<string, string>;
	workerVramMb: Record<string, number | null>;
	vramSource: string;
	vramUsedMb: number | null;
	vramFreeMb: number | null;
	queueDepth: number;
	inFlight: number;
	unloadInSeconds: number | null;
	lastUsedAt: number | null;
};

export type ModelSummary = {
	engineId: string;
	alias: string;
	hfRepo: string;
	qualityTier: string;
	estVramGb: number;
	isDefault: boolean;
	downloaded: boolean;
	loaded: boolean;
	mode: ModelMode;
	supportsInstruct: boolean;
	presetSpeakers: string[];
	kind: EngineKind;
};

export type EngineSummary = {
	id: string;
	displayName: string;
	backend: string;
	backendVersion: string;
	installed: boolean;
	kind: EngineKind;
	device: string;
	defaultDevice: string | null;
	supportedDevices: string[];
	resolvedDevice: string | null;
	customVoice: boolean;
	voiceDesign: boolean;
	instructControl: boolean;
	wordTimestamps: boolean;
	hotwords: boolean;
	multilingual: boolean;
	languages: string[];
	models: ModelSummary[];
	whyUnavailable: string | null;
};

export type VoiceEngineSupport = {
	engineId: string;
	modelAlias: string;
	maxRefSamples: number;
};

export type VoiceSummary = {
	id: string;
	name: string;
	kind: VoiceSource;
	source: VoiceSource;
	language: string;
	notes: string;
	durationSec: number;
	createdAt: number;
	sampleCount: number;
	refText: string;
	xVectorOnly: boolean;
	warnings: string[];
	designInstruct: string;
	consentRecordedAt: number | null;
	hasPreview: boolean;
	engineSupport: VoiceEngineSupport[];
};

export type VoiceSample = {
	id: string;
	durationSec: number;
	refText: string;
	isPrimary: boolean;
	createdAt: number;
};

export type VoiceDetail = {
	voice: VoiceSummary;
	samples: VoiceSample[];
};

export type PresetVoice = {
	presetId: string;
	engineId: string;
	modelAlias: string;
	speaker: string;
	displayName: string;
	languages: string[];
};

export type VoicePreview = {
	artifactId: string;
	downloadUrl: string;
	durationSec: number;
	engineId: string;
	modelAlias: string;
	voiceId: string | null;
	speaker: string | null;
	cached: boolean;
};

export type QualitySeverity = 'info' | 'warning' | 'error';

export type QualityWarning = {
	code: string;
	severity: QualitySeverity;
	message: string;
	suggestion: string;
};

export type ClippingReport = {
	detected: boolean;
	clippedSamples: number;
	firstAtSec: number | null;
};

export type DraftQuality = {
	durationSec: number;
	peakDbfs: number;
	rmsDbfs: number;
	truePeakDbtp: number | null;
	integratedLufs: number | null;
	silenceRatio: number;
	clipping: ClippingReport;
	warnings: QualityWarning[];
};

export type VoiceDraft = {
	draftId: string;
	createdAt: number;
	expiresAt: number;
	durationSec: number;
	sampleRate: number;
	channels: number;
	peaks: number[];
	quality: DraftQuality;
	suggestedTranscript: string | null;
	suggestedTranscriptLanguage: string | null;
	suggestedTranscriptEngine: string | null;
	suggestedTranscriptError: ApiErrorPayload | null;
};

export type HistoryKind = 'speak' | 'script' | 'design' | 'transcribe';

export type HistoryItem = {
	id: string;
	createdAt: number;
	kind: HistoryKind;
	status: 'succeeded' | 'failed';
	favorite: boolean;
	text: string;
	voiceId: string | null;
	voiceName: string;
	speaker: string | null;
	engineId: string;
	modelAlias: string;
	device: string | null;
	seed: number | null;
	take: number | null;
	durationSec: number;
	format: string;
	artifactId: string | null;
	audioUrl: string;
	captureId: string | null;
	rerenderable: boolean;
	error: ApiErrorPayload | null;
};

export type HistoryPage = {
	items: HistoryItem[];
	nextCursor: string | null;
};

export type CaptureSummary = {
	id: string;
	createdAt: number;
	title: string;
	transcript: string;
	language: string;
	durationSec: number;
	segmentsCount: number;
	engineId: string;
	modelAlias: string;
	source: 'upload' | 'path';
	audioUrl: string;
	truncated: boolean;
	segmentsOmitted: number;
};

export type TranscribeWord = {
	word: string;
	startSec: number;
	endSec: number;
};

export type TranscribeSegment = {
	index: number;
	startSec: number;
	endSec: number;
	text: string;
	words: TranscribeWord[];
};

export type CaptureDetail = CaptureSummary & {
	segments: TranscribeSegment[];
	fullSegmentsAvailable: boolean;
};

export type CapturePage = {
	items: CaptureSummary[];
	nextCursor: string | null;
};

export type TranscribeResult = {
	text: string;
	language: string;
	durationSec: number;
	segmentsCount: number;
	segments: TranscribeSegment[];
	engineId: string;
	modelAlias: string;
	backendName: string;
	backendVersion: string;
	elapsedMs: number;
	realtimeMultiple: number | null;
	truncated: boolean;
	segmentsOmitted: number;
	transcriptJsonPath: string | null;
	srtPath: string | null;
	vttPath: string | null;
	captureId: string | null;
};

export type Job = {
	jobId: string;
	kind: string;
	status: JobStatus;
	progress: number;
	message: string;
	createdAt: number;
	updatedAt: number;
	result: Record<string, unknown> | null;
	error: ApiErrorPayload | null;
};

export type JobStarted = {
	jobId: string;
	kind: string;
};

export type SettingEntry = {
	key: string;
	value: unknown;
	kind: 'bool' | 'int' | 'float' | 'str' | 'select' | 'readonly';
	group: string;
	label: string;
	description: string;
	editable: boolean;
	restartRequired: boolean;
	choices: string[];
	source: 'env' | 'file' | 'default' | 'readonly';
	shadowedByEnv: boolean;
};

export type SettingsSnapshot = {
	settings: SettingEntry[];
	values: Record<string, unknown>;
	configPath: string;
	restartRequired: boolean;
};

export type DiagnosticCheck = {
	name: string;
	status: 'PASS' | 'INFO' | 'WARN' | 'FAIL';
	detail: string;
	fix: string;
};

export type Diagnostics = {
	checkedAt: number;
	checks: DiagnosticCheck[];
};

export type LexiconScope = {
	scope: string;
	path: string;
	entries: Record<string, string>;
};

export type LexiconSnapshot = {
	project: string | null;
	voiceId: string | null;
	scopes: LexiconScope[];
	mergedEntries: number;
	effective: Record<string, string>;
	pronunciationHash: string;
};

export type CacheStats = {
	cacheDir: string;
	entries: number;
	totalBytes: number;
	maxBytes: number;
};

export type CacheClearResult = {
	filesDeleted: number;
	bytesFreed: number;
};

export type AuthCheck = {
	authenticated: boolean;
	authRequired: boolean;
};

export type ServerInfo = {
	version: string;
	protocolVersion: number;
	dataDir: string;
	home: string;
	inboxDir: string;
	artifactsDir: string;
	enginesDir: string;
	defaultEngine: string;
	defaultAsrEngine: string;
	defaultVoiceId: string;
	voicesCount: number;
	historyCount: number;
	capturesCount: number;
	nextSteps: string[];
};

export type UnloadResult = {
	unloaded: string[];
	cancelledJobs: number;
};

export type VoicePreviewRequest = {
	text: string;
	engine?: string | null;
	model?: string | null;
};

export type PresetPreviewRequest = VoicePreviewRequest & {
	engine: string;
	model: string;
	speaker: string;
};

export type DraftCreateResult = VoiceDraft;

export type DraftTrim = {
	startSec: number;
	endSec: number;
};

export type CloneVoiceRequest = {
	name: string;
	draft_id: string;
	trim: DraftTrim | null;
	ref_text: string;
	x_vector_only: boolean;
	language: string;
	notes: string;
	consent: boolean;
};

export type AddSampleRequest = {
	draft_id: string;
	trim: DraftTrim | null;
	ref_text: string;
	x_vector_only: boolean;
	make_primary: boolean;
	consent: boolean;
};

export type SpeakConfig = {
	mode: ModelMode;
	engineId: string;
	modelAlias: string;
	voiceId: string;
	presetId: string;
	speaker: string;
	instruct: string;
	language: string;
	format: AudioFormat;
	speed: number;
	quality: 'high' | 'fast';
	take: number | null;
	seed: number | null;
	markup: boolean;
	spellIdentifiers: boolean;
	paragraphGapMs: number;
	project: string;
};

export type SpeakRequest = {
	text: string;
	voice?: string | null;
	speaker?: string | null;
	instruct: string;
	engine?: string | null;
	model?: string | null;
	quality: 'high' | 'fast';
	language: string;
	format: AudioFormat;
	seed?: number | null;
	speed: number;
	take?: number | null;
	markup: boolean;
	spell_identifiers: boolean;
	paragraph_gap_ms: number;
	project?: string | null;
};

export type SynthesisResult = {
	artifactId: string;
	filePath: string;
	durationSec: number;
	sampleRate: number;
	format: string;
	engineId: string;
	modelAlias: string;
	backendName: string;
	backendVersion: string;
	voiceId: string | null;
	mode: ModelMode;
	speaker: string | null;
	cached: boolean;
	downloadUrl: string;
	chunksUsed: number;
	elapsedMs: number;
	take: number | null;
	realtimeMultiple: number | null;
};

export type DesignCandidate = {
	take: number;
	seed: number | null;
	filePath: string;
	downloadUrl: string;
	durationSec: number;
};

export type DesignResult = {
	voiceId: string | null;
	name: string;
	previewFilePath: string;
	durationSec: number;
	source: 'designed';
	designInstruct: string;
	engineId: string;
	modelAlias: string;
	candidates: DesignCandidate[];
};

export type ClipSummary = {
	id: string;
	title: string;
	subtitle: string;
	url: string;
	durationSec: number;
	engineId: string;
	modelAlias: string;
	cached: boolean;
	elapsedMs: number;
	realtimeMultiple: number | null;
	format: string;
	createdAt: number;
	voiceId: string | null;
	speaker: string | null;
	mode: ModelMode;
};

export type StudioEvent = {
	id: string;
	event: string;
	data: Record<string, unknown>;
};

export type EventConnection = 'idle' | 'connecting' | 'open' | 'closed';
