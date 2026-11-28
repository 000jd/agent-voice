/** Typed test fixtures. One shape, shared by every unit test. */
import type {
	ClipSummary,
	EngineSummary,
	GpuStatus,
	HistoryItem,
	Job,
	ModelSummary,
	PresetVoice,
	VoiceSummary
} from '#lib/api/types.js';

export function makeModel(overrides: Partial<ModelSummary> = {}): ModelSummary {
	return {
		engineId: 'mock',
		alias: 'mock-tone',
		hfRepo: 'local/mock-tone',
		qualityTier: 'fast',
		estVramGb: 0,
		isDefault: true,
		downloaded: true,
		loaded: false,
		mode: 'clone',
		supportsInstruct: false,
		presetSpeakers: [],
		kind: 'tts',
		...overrides
	};
}

export function makeEngine(overrides: Partial<EngineSummary> = {}): EngineSummary {
	return {
		id: 'mock',
		displayName: 'Mock',
		backend: 'mock',
		backendVersion: '1',
		installed: true,
		kind: 'tts',
		device: 'cpu',
		defaultDevice: 'cpu',
		supportedDevices: ['cpu'],
		resolvedDevice: null,
		customVoice: true,
		voiceDesign: true,
		instructControl: true,
		wordTimestamps: false,
		hotwords: false,
		multilingual: false,
		languages: ['Auto', 'English'],
		models: [makeModel()],
		whyUnavailable: null,
		...overrides
	};
}

export function makeVoice(overrides: Partial<VoiceSummary> = {}): VoiceSummary {
	return {
		id: 'voice-1',
		name: 'Narrator',
		kind: 'cloned',
		source: 'cloned',
		language: 'English',
		notes: '',
		durationSec: 8.4,
		createdAt: 1_700_000_000,
		sampleCount: 1,
		refText: 'A reference transcript.',
		xVectorOnly: false,
		warnings: [],
		designInstruct: '',
		consentRecordedAt: 1_700_000_000,
		hasPreview: false,
		engineSupport: [],
		...overrides
	};
}

export function makePreset(overrides: Partial<PresetVoice> = {}): PresetVoice {
	return {
		presetId: 'mock:mock-custom:Ryan',
		engineId: 'mock',
		modelAlias: 'mock-custom',
		speaker: 'Ryan',
		displayName: 'Ryan',
		languages: ['English'],
		...overrides
	};
}

export function makeClip(overrides: Partial<ClipSummary> = {}): ClipSummary {
	return {
		id: 'clip-1',
		title: 'A quiet line',
		subtitle: 'mock · mock-custom',
		url: '/audio/clip-1',
		durationSec: 6.15,
		engineId: 'mock',
		modelAlias: 'mock-custom',
		cached: false,
		elapsedMs: 120,
		realtimeMultiple: 2,
		format: 'wav',
		createdAt: 1_700_000_000,
		voiceId: null,
		speaker: 'Ryan',
		mode: 'custom_voice',
		...overrides
	};
}

export function makeGpu(overrides: Partial<GpuStatus> = {}): GpuStatus {
	return {
		activeEngine: null,
		loadedModels: [],
		workers: {},
		cpuEngines: [],
		workerDevices: {},
		workerVramMb: {},
		vramSource: 'none',
		vramUsedMb: null,
		vramFreeMb: null,
		queueDepth: 0,
		inFlight: 0,
		unloadInSeconds: null,
		lastUsedAt: null,
		...overrides
	};
}

export function makeHistoryItem(overrides: Partial<HistoryItem> = {}): HistoryItem {
	return {
		id: 'entry-1',
		createdAt: 1_700_000_000,
		kind: 'speak',
		status: 'succeeded',
		favorite: false,
		text: 'A quiet line',
		voiceId: null,
		voiceName: '',
		speaker: 'Ryan',
		engineId: 'mock',
		modelAlias: 'mock-custom',
		device: 'cpu',
		seed: null,
		take: null,
		durationSec: 6.15,
		format: 'wav',
		artifactId: 'clip-1',
		audioUrl: '/audio/clip-1',
		captureId: null,
		rerenderable: true,
		error: null,
		...overrides
	};
}

export function makeJob(overrides: Partial<Job> = {}): Job {
	return {
		jobId: 'job-1',
		kind: 'speak',
		status: 'pending',
		progress: 0,
		message: '',
		createdAt: 1_700_000_000,
		updatedAt: 1_700_000_000,
		result: null,
		error: null,
		...overrides
	};
}
