/** Voice library presentation helpers: monograms, labels, compatibility, search. */
import type { EngineSummary, ModelSummary, PresetVoice, VoiceSummary } from '#lib/api/types.js';

export const VOICE_FILTERS = ['all', 'cloned', 'designed', 'preset'] as const;
export type VoiceFilter = (typeof VOICE_FILTERS)[number];

export const VOICE_FILTER_LABELS: Record<VoiceFilter, string> = {
	all: 'All',
	cloned: 'Cloned',
	designed: 'Designed',
	preset: 'Presets'
};

export function initials(name: string): string {
	const words = name
		.replace(/[^\p{L}\p{N} ]/gu, ' ')
		.split(/\s+/)
		.filter(Boolean);
	if (words.length === 0) return 'AV';
	if (words.length === 1) return (words[0] ?? '').slice(0, 2).toUpperCase();
	return `${(words[0] ?? '')[0] ?? ''}${(words[1] ?? '')[0] ?? ''}`.toUpperCase();
}

export function voiceKindLabel(voice: VoiceSummary): string {
	return voice.kind === 'designed' ? 'Designed' : 'Cloned';
}

export function voiceLanguageLabel(voice: VoiceSummary): string {
	return voice.language && voice.language.trim() ? voice.language : 'Auto';
}

export function matchesQuery(voice: VoiceSummary, query: string): boolean {
	const needle = query.trim().toLowerCase();
	if (!needle) return true;
	return (
		voice.name.toLowerCase().includes(needle) ||
		voice.refText.toLowerCase().includes(needle) ||
		voice.designInstruct.toLowerCase().includes(needle) ||
		voice.language.toLowerCase().includes(needle)
	);
}

export function filterVoices(
	voices: VoiceSummary[],
	filter: VoiceFilter,
	query: string
): VoiceSummary[] {
	if (filter === 'preset') return [];
	return voices.filter((voice) => {
		if (filter !== 'all' && voice.kind !== filter) return false;
		return matchesQuery(voice, query);
	});
}

export function cloneModel(engine: EngineSummary): ModelSummary | null {
	return (
		engine.models.find((model) => model.mode === 'clone' && model.downloaded) ??
		engine.models.find((model) => model.mode === 'clone') ??
		null
	);
}

/** TTS engines that can speak a cloned reference, installed first. */
export function cloneCapableEngines(engines: EngineSummary[]): EngineSummary[] {
	return engines
		.filter((engine) => engine.kind === 'tts' && cloneModel(engine) !== null)
		.sort((left, right) => Number(right.installed) - Number(left.installed));
}

export function voiceCompatibility(
	voice: VoiceSummary,
	engines: EngineSummary[]
): { engineId: string; modelAlias: string } | null {
	const capable = cloneCapableEngines(engines);
	if (capable.length === 0) return null;
	for (const support of voice.engineSupport) {
		const engine = capable.find((item) => item.id === support.engineId);
		if (!engine) continue;
		const model = engine.models.find((item) => item.alias === support.modelAlias);
		if (model) return { engineId: engine.id, modelAlias: model.alias };
	}
	const engine = capable[0];
	if (!engine) return null;
	const model = cloneModel(engine);
	if (!model) return null;
	return { engineId: engine.id, modelAlias: model.alias };
}

/** Presets arrive one row per speaker; the UI shows exactly one card per speaker. */
export function uniquePresets(presets: PresetVoice[]): PresetVoice[] {
	const seen = new Set<string>();
	const result: PresetVoice[] = [];
	for (const preset of presets) {
		const key = `${preset.engineId}:${preset.speaker}`;
		if (seen.has(key)) continue;
		seen.add(key);
		result.push(preset);
	}
	return result;
}

export function filterPresets(
	presets: PresetVoice[],
	filter: VoiceFilter,
	query: string
): PresetVoice[] {
	if (filter !== 'all' && filter !== 'preset') return [];
	const needle = query.trim().toLowerCase();
	const unique = uniquePresets(presets);
	if (!needle) return unique;
	return unique.filter(
		(preset) =>
			preset.displayName.toLowerCase().includes(needle) ||
			preset.speaker.toLowerCase().includes(needle) ||
			preset.engineId.toLowerCase().includes(needle) ||
			preset.modelAlias.toLowerCase().includes(needle) ||
			preset.languages.some((language) => language.toLowerCase().includes(needle))
	);
}

export function presetId(preset: PresetVoice): string {
	return `${preset.engineId}:${preset.modelAlias}:${preset.speaker}`;
}

export function presetModel(
	engines: EngineSummary[],
	preset: PresetVoice
): { engine: EngineSummary; model: ModelSummary } | null {
	const engine = engines.find((item) => item.id === preset.engineId);
	if (!engine) return null;
	const model = engine.models.find((item) => item.alias === preset.modelAlias);
	if (!model) return null;
	return { engine, model };
}

export function presetAvailability(
	engines: EngineSummary[],
	preset: PresetVoice
): 'ready' | 'needs-download' | 'unavailable' {
	const found = presetModel(engines, preset);
	if (!found) return 'unavailable';
	if (!found.engine.installed) return 'unavailable';
	return found.model.downloaded ? 'ready' : 'needs-download';
}

export const PREVIEW_TEXT = 'The room is quiet. Here is how this voice sounds.';

export function totalDurationSeconds(voices: VoiceSummary[]): number {
	return voices.reduce(
		(sum, voice) => sum + (Number.isFinite(voice.durationSec) ? voice.durationSec : 0),
		0
	);
}
