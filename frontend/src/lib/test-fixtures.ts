import type { ClipSummary, EngineSummary, ModelSummary, VoiceSummary } from '#lib/api/types.js';

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
		resolvedDevice: 'cpu',
		customVoice: true,
		voiceDesign: true,
		instructControl: true,
		wordTimestamps: false,
		hotwords: false,
		multilingual: false,
		languages: ['Auto', 'English'],
		models: [],
		whyUnavailable: null,
		...overrides
	};
}

export function makeVoice(overrides: Partial<VoiceSummary> = {}): VoiceSummary {
	return {
		id: 'voice-1',
		name: 'Narrator',
		refText: 'A reference line.',
		xVectorOnly: false,
		durationSec: 4.2,
		createdAt: 1_700_000_000,
		warnings: [],
		source: 'cloned',
		designInstruct: '',
		...overrides
	};
}

export function makeClip(overrides: Partial<ClipSummary> = {}): ClipSummary {
	return {
		id: 'clip-1',
		title: 'A test render',
		subtitle: 'mock · mock-tone',
		url: '/audio/clip-1',
		durationSec: 2.4,
		engineId: 'mock',
		modelAlias: 'mock-tone',
		cached: false,
		elapsedMs: 120,
		realtimeMultiple: 2.4,
		format: 'wav',
		createdAt: 1_700_000_000,
		voiceId: null,
		speaker: 'Ryan',
		mode: 'custom_voice',
		...overrides
	};
}
