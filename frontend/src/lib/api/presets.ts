/** Built-in preset speakers and their previews. */
import { api, unwrap } from './client.js';
import { parsePresetsPayload, parseVoicePreview } from './guards.js';
import type { PresetVoice, VoicePreview } from './types.js';

export async function listPresets(): Promise<PresetVoice[]> {
	const result = (await api['/v1/presets'].GET()) as Parameters<typeof unwrap>[0];
	return parsePresetsPayload(unwrap(result, '/v1/presets'));
}

export async function previewPreset(input: {
	engine: string;
	model: string;
	speaker: string;
	text: string;
}): Promise<VoicePreview> {
	const result = (await api['/v1/presets/preview'].POST({
		body: {
			text: input.text,
			engine: input.engine,
			model: input.model,
			speaker: input.speaker
		}
	})) as Parameters<typeof unwrap>[0];
	return parseVoicePreview(unwrap(result, '/v1/presets/preview'));
}
