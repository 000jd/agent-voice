import { fireEvent, render, screen } from '@testing-library/svelte';
import { expect, test } from 'vitest';
import { createDefaultSpeakConfig } from '#lib/speak.js';
import { studio } from '#lib/stores/studio.svelte.js';
import { makeEngine, makeModel } from '#lib/test-fixtures.js';
import SpeakInspector from './speak-inspector.svelte';

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

test('reflects and changes the speak capability state', async () => {
	studio.engines = [mock];
	studio.voices = [];
	studio.sessionClips = [];
	studio.catalogLoading = false;
	studio.speak = createDefaultSpeakConfig([mock], []);

	render(SpeakInspector);
	expect(screen.getByLabelText('Engine')).toHaveValue('mock');
	expect(screen.getByLabelText('Model')).toHaveValue('mock-custom');
	expect(screen.getByLabelText('Preset speaker')).toHaveValue('Ryan');
	expect(screen.getByRole('button', { name: /^Preset$/ })).toHaveAttribute('aria-pressed', 'true');

	await fireEvent.click(screen.getByRole('button', { name: /^Design$/ }));
	expect(studio.speak.mode).toBe('voice_design');
	expect(studio.speak.modelAlias).toBe('mock-design');
	expect(screen.getByLabelText('Voice description')).toBeInTheDocument();

	await fireEvent.click(screen.getByRole('button', { name: /^Clone$/ }));
	expect(studio.speak.mode).toBe('clone');
	expect(studio.speak.modelAlias).toBe('mock-tone');
	expect(screen.getByText(/No registered voices\./)).toBeInTheDocument();
});
