import { describe, expect, it } from 'vitest';
import { SseParser } from '#lib/api/events.js';
import type { EngineSummary, PresetVoice, VoiceSummary } from '#lib/api/types.js';
import { buildSpeakRequest, defaultSpeakConfig, validateSpeak } from '#lib/speak.js';
import { filterPresets, initials, uniquePresets, voiceCompatibility } from '#lib/voices.js';

function voice(overrides: Partial<VoiceSummary> = {}): VoiceSummary {
	return {
		id: '0123456789ab',
		name: 'Ada Narrator',
		kind: 'cloned',
		source: 'cloned',
		language: 'English',
		notes: '',
		durationSec: 6.4,
		createdAt: 1,
		sampleCount: 1,
		refText: 'A clean line.',
		xVectorOnly: false,
		warnings: [],
		designInstruct: '',
		consentRecordedAt: 1,
		hasPreview: true,
		engineSupport: [],
		...overrides
	};
}

function engine(overrides: Partial<EngineSummary> = {}): EngineSummary {
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
		instructControl: false,
		wordTimestamps: false,
		hotwords: false,
		multilingual: false,
		languages: ['Auto', 'English'],
		models: [
			{
				engineId: 'mock',
				alias: 'mock-tone',
				hfRepo: 'local/mock',
				qualityTier: 'fast',
				estVramGb: 0,
				isDefault: true,
				downloaded: true,
				loaded: false,
				supportsInstruct: false,
				presetSpeakers: [],
				mode: 'clone',
				kind: 'tts'
			}
		],
		whyUnavailable: null,
		...overrides
	};
}

describe('SSE parser', () => {
	it('parses events across chunks and ignores comments', () => {
		const parser = new SseParser();
		expect(parser.push(': heartbeat\n\nid: 4\nevent: job\n')).toEqual([]);
		expect(parser.push('data: {"ok":true}\n\n')).toEqual([
			{ id: '4', event: 'job', data: '{"ok":true}' }
		]);
	});

	it('joins multiple data lines', () => {
		const parser = new SseParser();
		expect(parser.push('data: one\ndata: two\n\n')).toEqual([
			{ id: '', event: 'message', data: 'one\ntwo' }
		]);
	});
});

describe('voice helpers', () => {
	it('builds a two-letter monogram', () => {
		expect(initials('Ada Narrator')).toBe('AN');
		expect(initials('')).toBe('AV');
	});

	it('resolves compatible clone support', () => {
		const support = voice({
			engineSupport: [{ engineId: 'mock', modelAlias: 'mock-tone', maxRefSamples: 0 }]
		});
		expect(voiceCompatibility(support, [engine()])).toEqual({
			engineId: 'mock',
			modelAlias: 'mock-tone'
		});
	});

	it('deduplicates presets by engine and speaker', () => {
		const preset = (displayName: string): PresetVoice => ({
			presetId: `mock:mock-custom:${displayName}`,
			engineId: 'mock',
			modelAlias: 'mock-custom',
			speaker: displayName,
			displayName,
			languages: ['English']
		});
		expect(uniquePresets([preset('Ryan'), preset('Ryan')])).toHaveLength(1);
		expect(filterPresets([preset('Ryan'), preset('Serena')], 'all', 'ser')).toHaveLength(1);
	});
});

describe('speak request', () => {
	it('keeps a clone voice and its engine in the request', () => {
		const config = {
			...defaultSpeakConfig(),
			engineId: 'mock',
			modelAlias: 'mock-tone',
			mode: 'clone' as const,
			voiceId: '0123456789ab'
		};
		const request = buildSpeakRequest('Hello.', config);
		expect(request.voice).toBe('0123456789ab');
		expect(request.speaker).toBeUndefined();
		expect(request.engine).toBe('mock');
	});

	it('validates empty text and clone without a voice', () => {
		expect(validateSpeak('   ', defaultSpeakConfig())).toMatch(/write a line/i);
		const config = {
			...defaultSpeakConfig(),
			engineId: 'mock',
			modelAlias: 'mock-tone',
			mode: 'clone' as const
		};
		expect(validateSpeak('Hello', config)).toMatch(/pick a voice/i);
	});
});
