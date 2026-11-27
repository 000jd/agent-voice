import type {
	ClipSummary,
	DesignCandidate,
	DesignVoiceRequest,
	EngineSummary,
	VoiceSource,
	VoiceSummary
} from '#lib/api/types.js';

export type BuiltinPreset = {
	kind: 'preset';
	id: string;
	name: string;
	engineId: string;
	engineName: string;
	modelAlias: string;
	speaker: string;
	supportsInstruct: boolean;
};

export type VoiceItem =
	| { kind: 'voice'; id: string; name: string; voice: VoiceSummary }
	| { kind: 'preset'; id: string; name: string; preset: BuiltinPreset };

export type PresetGroup = {
	id: string;
	engineId: string;
	engineName: string;
	modelAlias: string;
	speakers: string[];
	supportsInstruct: boolean;
};

export const VOICE_FILTERS = ['all', 'cloned', 'designed', 'preset'] as const;
export type VoiceFilter = (typeof VOICE_FILTERS)[number];

export const DEFAULT_DESIGN_SAMPLE =
	'Welcome! Today we are going to break down this concept step by step, clearly and intuitively.';

export function builtinPresets(engines: EngineSummary[]): BuiltinPreset[] {
	const presets: BuiltinPreset[] = [];
	for (const engine of engines) {
		if (engine.kind !== 'tts') continue;
		for (const model of engine.models) {
			for (const speaker of model.presetSpeakers) {
				presets.push({
					kind: 'preset',
					id: `preset:${engine.id}:${model.alias}:${speaker}`,
					name: `${speaker} — ${engine.displayName}`,
					engineId: engine.id,
					engineName: engine.displayName,
					modelAlias: model.alias,
					speaker,
					supportsInstruct: model.supportsInstruct
				});
			}
		}
	}
	return presets;
}

export function builtinPresetGroups(engines: EngineSummary[]): PresetGroup[] {
	const groups: PresetGroup[] = [];
	for (const engine of engines) {
		if (engine.kind !== 'tts') continue;
		for (const model of engine.models) {
			if (model.presetSpeakers.length === 0) continue;
			groups.push({
				id: `preset-group:${engine.id}:${model.alias}`,
				engineId: engine.id,
				engineName: engine.displayName,
				modelAlias: model.alias,
				speakers: model.presetSpeakers,
				supportsInstruct: model.supportsInstruct
			});
		}
	}
	return groups;
}

export function filterPresetGroups(groups: PresetGroup[], query: string): PresetGroup[] {
	const needle = query.trim().toLowerCase();
	if (!needle) return groups;
	return groups.filter(
		(group) =>
			group.engineName.toLowerCase().includes(needle) ||
			group.engineId.toLowerCase().includes(needle) ||
			group.modelAlias.toLowerCase().includes(needle) ||
			group.speakers.some((speaker) => speaker.toLowerCase().includes(needle))
	);
}

export function voiceItems(voices: VoiceSummary[], engines: EngineSummary[]): VoiceItem[] {
	const items: VoiceItem[] = voices.map((voice) => ({
		kind: 'voice',
		id: voice.id,
		name: voice.name,
		voice
	}));
	for (const preset of builtinPresets(engines)) {
		items.push({ kind: 'preset', id: preset.id, name: preset.name, preset });
	}
	return items;
}

export function filterVoiceItems(
	items: VoiceItem[],
	filter: VoiceFilter,
	query: string
): VoiceItem[] {
	const needle = query.trim().toLowerCase();
	return items.filter((item) => {
		if (filter !== 'all') {
			if (item.kind === 'preset') {
				if (filter !== 'preset') return false;
			} else if (item.voice.source !== filter) {
				return false;
			}
		}
		if (!needle) return true;
		if (item.kind === 'preset') {
			return (
				item.preset.name.toLowerCase().includes(needle) ||
				item.preset.engineId.toLowerCase().includes(needle) ||
				item.preset.modelAlias.toLowerCase().includes(needle)
			);
		}
		return (
			item.voice.name.toLowerCase().includes(needle) ||
			item.voice.refText.toLowerCase().includes(needle) ||
			item.voice.designInstruct.toLowerCase().includes(needle)
		);
	});
}

export function countFilter(items: VoiceItem[], filter: VoiceFilter): number {
	if (filter === 'all') return items.length;
	return items.filter((item) =>
		item.kind === 'preset' ? filter === 'preset' : item.voice.source === filter
	).length;
}

export function voiceSourceLabel(source: VoiceSource): string {
	return source === 'designed' ? 'Designed' : 'Cloned';
}

const TONES = ['cobalt', 'navy', 'steel', 'amber'] as const;
export type VoiceTone = (typeof TONES)[number];

export function voiceTone(name: string): VoiceTone {
	let hash = 0;
	for (let index = 0; index < name.length; index += 1) {
		hash = (hash * 31 + name.charCodeAt(index)) >>> 0;
	}
	return TONES[hash % TONES.length] ?? 'cobalt';
}

export function initials(name: string): string {
	const words = name
		.replace(/[^\p{L}\p{N} ]/gu, ' ')
		.split(/\s+/)
		.filter(Boolean);
	if (words.length === 0) return 'AV';
	if (words.length === 1) return words[0]!.slice(0, 2).toUpperCase();
	return `${words[0]![0] ?? ''}${words[1]![0] ?? ''}`.toUpperCase();
}

export function designRequest(input: {
	name: string;
	instruct: string;
	sampleText: string;
	language: string;
	candidates: number;
	engineId: string;
	modelAlias: string;
	take?: number | null;
	seed?: number | null;
	saveAsClone: boolean;
}): DesignVoiceRequest {
	return {
		name: input.name.trim(),
		instruct: input.instruct.trim(),
		sample_text: input.sampleText.trim() || DEFAULT_DESIGN_SAMPLE,
		language: input.language,
		save_as_clone: input.saveAsClone,
		candidates: input.candidates,
		take: input.take ?? null,
		seed: input.seed ?? null,
		overwrite: false,
		engine: input.engineId || null,
		model: input.modelAlias || null,
		quality: 'high'
	};
}

export function findDesignEngine(engines: EngineSummary[]): EngineSummary | null {
	return (
		engines.find(
			(engine) =>
				engine.kind === 'tts' && engine.models.some((model) => model.mode === 'voice_design')
		) ?? null
	);
}

export function findDesignModel(engine: EngineSummary | null): string {
	if (!engine) return '';
	return (
		engine.models.find((model) => model.mode === 'voice_design' && model.downloaded)?.alias ??
		engine.models.find((model) => model.mode === 'voice_design')?.alias ??
		''
	);
}

export function clipFromCandidate(
	candidate: DesignCandidate,
	name: string,
	engineId: string,
	modelAlias: string
): ClipSummary {
	return {
		id: `design:${engineId}:${modelAlias}:${candidate.take}:${candidate.seed ?? 'auto'}`,
		title: `${name} · take ${candidate.take}`,
		subtitle: `${engineId} · ${modelAlias}`,
		url: candidate.downloadUrl || candidate.filePath,
		durationSec: candidate.durationSec,
		engineId,
		modelAlias,
		cached: false,
		elapsedMs: 0,
		realtimeMultiple: null,
		format: 'wav',
		createdAt: Date.now(),
		voiceId: null,
		speaker: null,
		mode: 'voice_design'
	};
}
