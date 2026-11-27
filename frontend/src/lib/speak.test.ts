import { expect, test } from 'vitest';
import {
	buildSpeakRequest,
	createDefaultSpeakConfig,
	defaultMode,
	modesForEngine,
	pickSpeaker,
	validateSpeak
} from '#lib/speak.js';
import { makeEngine, makeModel, makeVoice } from '#lib/test-fixtures.js';

const mock = makeEngine({
	models: [
		makeModel({ mode: 'clone', alias: 'mock-tone' }),
		makeModel({
			mode: 'custom_voice',
			alias: 'mock-custom',
			supportsInstruct: true,
			presetSpeakers: ['Ryan', 'Serena']
		}),
		makeModel({ mode: 'voice_design', alias: 'mock-design', supportsInstruct: true })
	]
});

test('discovers modes and chooses a runnable default without a voice', () => {
	expect(modesForEngine(mock)).toEqual(['clone', 'custom_voice', 'voice_design']);
	expect(defaultMode(mock, [])).toBe('custom_voice');
	const config = createDefaultSpeakConfig([mock], []);
	expect(config.engineId).toBe('mock');
	expect(config.mode).toBe('custom_voice');
	expect(config.modelAlias).toBe('mock-custom');
	expect(config.speaker).toBe('Ryan');
	expect(pickSpeaker(mock.models[1] ?? null)).toBe('Ryan');
});

test('uses a registered voice in clone mode and builds the right request', () => {
	const voice = makeVoice({ id: 'voice-1' });
	const config = createDefaultSpeakConfig([mock], [voice]);
	expect(config.mode).toBe('clone');
	expect(config.voiceId).toBe('voice-1');
	const request = buildSpeakRequest('  hello  ', config);
	expect(request.text).toBe('hello');
	expect(request.voice).toBe('voice-1');
	expect(request.speaker).toBeUndefined();
	expect(request.instruct).toBe('');
	expect(request.format).toBe('wav');
	expect(request.quality).toBe('high');
});

test('sends speaker and instruct only for preset mode', () => {
	const config = {
		...createDefaultSpeakConfig([mock], []),
		speaker: 'Serena',
		instruct: '  measured  '
	};
	const request = buildSpeakRequest('hello', config);
	expect(request.speaker).toBe('Serena');
	expect(request.instruct).toBe('measured');
	expect(request.voice).toBeUndefined();
});

test('sends instruct and no timbre for voice design', () => {
	const config = {
		...createDefaultSpeakConfig([mock], []),
		mode: 'voice_design' as const,
		modelAlias: 'mock-design',
		speaker: '',
		instruct: 'warm baritone'
	};
	const request = buildSpeakRequest('hello', config);
	expect(request.instruct).toBe('warm baritone');
	expect(request.speaker).toBeUndefined();
	expect(request.voice).toBeUndefined();
});

test('validates the mode-specific fields before a round trip', () => {
	const base = createDefaultSpeakConfig([mock], []);
	expect(validateSpeak('', base)).toMatch(/Write a line/);
	expect(validateSpeak('hello', { ...base, mode: 'clone', voiceId: '' })).toMatch(
		/registered voice/
	);
	expect(validateSpeak('hello', { ...base, mode: 'voice_design', instruct: '' })).toMatch(
		/needs a description/
	);
	expect(validateSpeak('hello', base)).toBeNull();
});
