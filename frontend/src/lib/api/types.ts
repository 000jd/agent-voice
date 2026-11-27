export type EngineKind = 'tts' | 'asr';
export type ModelMode = 'clone' | 'custom_voice' | 'voice_design';
export type VoiceSource = 'cloned' | 'designed';
export type AudioFormat = 'wav' | 'mp3' | 'opus';

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

export type VoiceSummary = {
	id: string;
	name: string;
	refText: string;
	xVectorOnly: boolean;
	durationSec: number;
	createdAt: number;
	warnings: string[];
	source: VoiceSource;
	designInstruct: string;
};

export type SpeakMode = ModelMode;

export type SpeakConfig = {
	mode: SpeakMode;
	engineId: string;
	modelAlias: string;
	voiceId: string;
	speaker: string;
	instruct: string;
	language: string;
	format: AudioFormat;
	speed: number;
	take: number | null;
	seed: number | null;
	markup: boolean;
	spellIdentifiers: boolean;
	paragraphGapMs: number;
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
};

export type DesignVoiceRequest = {
	name: string;
	instruct: string;
	sample_text: string;
	language: string;
	save_as_clone: boolean;
	candidates: number;
	take?: number | null;
	seed?: number | null;
	overwrite: boolean;
	engine?: string | null;
	model?: string | null;
	quality: 'high' | 'fast';
};

export type CloneVoiceRequest = {
	name: string;
	ref_audio_path: string;
	ref_text: string;
	x_vector_only: boolean;
};

export type SpeakResult = {
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
	streamingMode: string | null;
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
};

export type UnloadResult = {
	unloaded: string[];
	cancelledJobs: number;
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

export type ApiErrorPayload = {
	status: number;
	code: string;
	message: string;
	suggestion: string;
};
