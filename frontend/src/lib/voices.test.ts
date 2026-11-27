import { expect, test } from 'vitest';
import {
	builtinPresetGroups,
	clipFromCandidate,
	countFilter,
	designRequest,
	filterPresetGroups,
	filterVoiceItems,
	initials,
	voiceItems,
	voiceTone
} from '#lib/voices.js';
import { makeEngine, makeModel, makeVoice } from '#lib/test-fixtures.js';

const kokoro = makeEngine({
	id: 'kokoro',
	displayName: 'Kokoro',
	models: [
		makeModel({
			engineId: 'kokoro',
			mode: 'custom_voice',
			alias: 'kokoro-82m',
			presetSpeakers: ['af_heart', 'am_adam']
		})
	]
});
const mock = makeEngine({
	models: [
		makeModel({
			engineId: 'mock',
			mode: 'custom_voice',
			alias: 'mock-custom',
			presetSpeakers: ['Ryan', 'Serena']
		})
	]
});
const cloned = makeVoice({ id: 'cloned', name: 'Narrator', source: 'cloned' });
const designed = makeVoice({
	id: 'designed',
	name: 'Late-night host',
	source: 'designed',
	designInstruct: 'warm and close'
});

test('filters user voices and preset groups without inventing rows', () => {
	const items = voiceItems([cloned, designed], []);
	expect(countFilter(items, 'all')).toBe(2);
	expect(filterVoiceItems(items, 'designed', '').map((item) => item.id)).toEqual(['designed']);
	expect(filterVoiceItems(items, 'all', 'late').map((item) => item.id)).toEqual(['designed']);

	const groups = builtinPresetGroups([kokoro, mock]);
	expect(groups).toHaveLength(2);
	expect(groups[0]?.speakers).toEqual(['af_heart', 'am_adam']);
	expect(filterPresetGroups(groups, 'am_adam')).toHaveLength(1);
	expect(filterPresetGroups(groups, 'missing')).toHaveLength(0);
});

test('builds the two-stage design request and candidate playback data', () => {
	const request = designRequest({
		name: '  narrator  ',
		instruct: '  warm  ',
		sampleText: '',
		language: 'English',
		candidates: 2,
		engineId: 'mock',
		modelAlias: 'mock-design',
		saveAsClone: false
	});
	expect(request.name).toBe('narrator');
	expect(request.instruct).toBe('warm');
	expect(request.candidates).toBe(2);
	expect(request.save_as_clone).toBe(false);
	expect(request.overwrite).toBe(false);
	expect(request.sample_text.length).toBeGreaterThan(20);

	const clip = clipFromCandidate(
		{
			take: 2,
			seed: 20014,
			filePath: '/tmp/take.wav',
			downloadUrl: '/audio/take2',
			durationSec: 4.2
		},
		'Narrator',
		'mock',
		'mock-design'
	);
	expect(clip.title).toBe('Narrator · take 2');
	expect(clip.url).toBe('/audio/take2');
	expect(clip.mode).toBe('voice_design');
});

test('monograms and tones are deterministic', () => {
	expect(initials('Late-night host')).toBe('LN');
	expect(voiceTone('Narrator')).toBe(voiceTone('Narrator'));
});
