/** Speak configuration: voice-first selection, engine/model derived, request builder. */
import type {
	EngineSummary,
	ModelMode,
	ModelSummary,
	PresetVoice,
	SpeakConfig,
	SpeakRequest,
	VoiceSummary
} from '#lib/api/types.js';
import { cloneCapableEngines, presetAvailability, voiceCompatibility } from '#lib/voices.js';

export const MODE_LABELS: Record<ModelMode, string> = {
	clone: 'Cloned voice',
	custom_voice: 'Preset speaker',
	voice_design: 'Designed voice'
};

export const ALL_MODES: readonly ModelMode[] = ['clone', 'custom_voice', 'voice_design'];

export function ttsEngines(engines: EngineSummary[]): EngineSummary[] {
	return engines.filter((engine) => engine.kind === 'tts');
}

export function asrEngines(engines: EngineSummary[]): EngineSummary[] {
	return engines.filter((engine) => engine.kind === 'asr');
}

export function findEngine(engines: EngineSummary[], id: string): EngineSummary | null {
	return engines.find((engine) => engine.id === id) ?? null;
}

export function modelsForMode(engine: EngineSummary | null, mode: ModelMode): ModelSummary[] {
	if (!engine) return [];
	return engine.models.filter((model) => model.mode === mode);
}

export function modesForEngine(engine: EngineSummary | null): ModelMode[] {
	if (!engine) return [];
	const modes = new Set<ModelMode>();
	for (const model of engine.models) modes.add(model.mode);
	return ALL_MODES.filter((mode) => modes.has(mode));
}

export function pickModel(engine: EngineSummary, mode: ModelMode): ModelSummary | null {
	const candidates = modelsForMode(engine, mode);
	if (candidates.length === 0) return null;
	return (
		candidates.find((model) => model.downloaded && model.isDefault) ??
		candidates.find((model) => model.downloaded) ??
		candidates.find((model) => model.isDefault) ??
		candidates[0] ??
		null
	);
}

export function defaultSpeakConfig(): SpeakConfig {
	return {
		mode: 'clone',
		engineId: '',
		modelAlias: '',
		voiceId: '',
		presetId: '',
		speaker: '',
		instruct: '',
		language: 'Auto',
		format: 'wav',
		speed: 1,
		quality: 'high',
		take: null,
		seed: null,
		markup: true,
		spellIdentifiers: false,
		paragraphGapMs: 0,
		project: ''
	};
}

export function languageFor(engine: EngineSummary | null, current: string): string {
	if (!engine || engine.languages.length === 0) return current || 'Auto';
	return engine.languages.includes(current) ? current : (engine.languages[0] ?? 'Auto');
}

export function configForVoice(
	config: SpeakConfig,
	voice: VoiceSummary,
	engines: EngineSummary[]
): SpeakConfig {
	const compatible = voiceCompatibility(voice, engines);
	if (!compatible) {
		return { ...config, mode: 'clone', voiceId: voice.id, presetId: '', speaker: '', instruct: '' };
	}
	const engine = findEngine(engines, compatible.engineId);
	return {
		...config,
		mode: 'clone',
		engineId: compatible.engineId,
		modelAlias: compatible.modelAlias,
		voiceId: voice.id,
		presetId: '',
		speaker: '',
		instruct: '',
		language: languageFor(engine, config.language)
	};
}

export function configForPreset(
	config: SpeakConfig,
	preset: PresetVoice,
	engines: EngineSummary[]
): SpeakConfig {
	const engine = findEngine(engines, preset.engineId);
	return {
		...config,
		mode: 'custom_voice',
		engineId: preset.engineId,
		modelAlias: preset.modelAlias,
		voiceId: '',
		speaker: preset.speaker,
		presetId: `${preset.engineId}:${preset.modelAlias}:${preset.speaker}`,
		instruct: '',
		language: languageFor(engine, config.language)
	};
}

/** Advanced engine override: keep the mode when the engine can, else re-derive. */
export function configForEngine(
	config: SpeakConfig,
	engine: EngineSummary,
	voices: VoiceSummary[],
	presets: PresetVoice[]
): SpeakConfig {
	const modes = modesForEngine(engine);
	const mode = modes.includes(config.mode) ? config.mode : (modes[0] ?? config.mode);
	const model = pickModel(engine, mode);
	let next: SpeakConfig = {
		...config,
		engineId: engine.id,
		mode,
		modelAlias: model?.alias ?? '',
		language: languageFor(engine, config.language)
	};
	if (mode === 'custom_voice') {
		const speakers = model?.presetSpeakers ?? [];
		if (!speakers.includes(next.speaker)) {
			const preset = presets.find(
				(item) => item.engineId === engine.id && item.modelAlias === model?.alias
			);
			next = {
				...next,
				speaker: speakers[0] ?? '',
				presetId: preset ? `${preset.engineId}:${preset.modelAlias}:${preset.speaker}` : ''
			};
		}
		next = { ...next, voiceId: '' };
	} else if (mode === 'clone') {
		const voice = voices.find((item) => item.id === next.voiceId) ?? voices[0];
		next = {
			...next,
			voiceId: voice?.id ?? '',
			speaker: '',
			presetId: ''
		};
	} else {
		next = { ...next, voiceId: '', speaker: '', presetId: '' };
	}
	return next;
}

export function configForModel(config: SpeakConfig, model: ModelSummary): SpeakConfig {
	return {
		...config,
		modelAlias: model.alias,
		mode: model.mode,
		speaker: model.mode === 'custom_voice' ? (model.presetSpeakers[0] ?? '') : '',
		voiceId: model.mode === 'clone' ? config.voiceId : '',
		presetId: ''
	};
}

/** Pick a sensible first selection: a cloned voice if a clone engine exists. */
export function autoConfig(
	engines: EngineSummary[],
	voices: VoiceSummary[],
	presets: PresetVoice[]
): SpeakConfig {
	const base = defaultSpeakConfig();
	const cloneEngines = cloneCapableEngines(engines).filter((engine) => engine.installed);
	const firstVoice = voices.find((voice) => voiceCompatibility(voice, engines) !== null);
	if (cloneEngines.length > 0 && firstVoice) {
		return configForVoice(base, firstVoice, engines);
	}
	const readyPreset = presets.find((preset) => presetAvailability(engines, preset) === 'ready');
	if (readyPreset) return configForPreset(base, readyPreset, engines);
	const engine = ttsEngines(engines).find((item) => item.installed) ?? ttsEngines(engines)[0];
	if (engine) {
		const model = engine.models[0];
		const mode = model?.mode ?? 'clone';
		const picked = pickModel(engine, mode);
		return {
			...base,
			engineId: engine.id,
			mode,
			modelAlias: picked?.alias ?? '',
			speaker: picked?.presetSpeakers[0] ?? '',
			language: languageFor(engine, base.language)
		};
	}
	return base;
}

/** Drop selections the catalog no longer contains (voice deleted, engine removed). */
export function ensureConfig(
	config: SpeakConfig,
	engines: EngineSummary[],
	voices: VoiceSummary[],
	presets: PresetVoice[]
): SpeakConfig {
	const engine = findEngine(engines, config.engineId);
	const voice = voices.find((item) => item.id === config.voiceId);
	const presetExists = presets.some(
		(item) =>
			item.engineId === config.engineId &&
			item.modelAlias === config.modelAlias &&
			item.speaker === config.speaker
	);
	if (!engine) return autoConfig(engines, voices, presets);
	if (config.mode === 'clone' && !voice) {
		const fallback = voices.find((item) => voiceCompatibility(item, engines) !== null);
		if (fallback) return configForVoice(config, fallback, engines);
		const readyPreset = presets.find((item) => presetAvailability(engines, item) === 'ready');
		if (readyPreset) return configForPreset(config, readyPreset, engines);
	}
	if (config.mode === 'custom_voice' && !presetExists) {
		const speakerExists = modelsForMode(engine, 'custom_voice').some((model) =>
			model.presetSpeakers.includes(config.speaker)
		);
		if (!speakerExists) {
			const model = pickModel(engine, 'custom_voice');
			if (model) {
				return {
					...config,
					modelAlias: model.alias,
					speaker: model.presetSpeakers[0] ?? ''
				};
			}
		}
	}
	const modelStillExists = engine.models.some((model) => model.alias === config.modelAlias);
	if (!modelStillExists) {
		const model = pickModel(engine, config.mode) ?? engine.models[0];
		if (model) return { ...config, modelAlias: model.alias, mode: model.mode };
	}
	return config;
}

export function validateSpeak(text: string, config: SpeakConfig): string | null {
	if (!text.trim()) return 'Write a line before generating.';
	if (text.length > 20_000) return 'The daemon accepts at most 20,000 characters.';
	if (!config.engineId) return 'Choose an engine in the inspector.';
	if (!config.modelAlias) return 'Choose a model in the inspector.';
	if (config.mode === 'clone' && !config.voiceId) {
		return 'Pick a voice first, or switch to a preset in the inspector.';
	}
	if (config.mode === 'custom_voice' && !config.speaker) return 'Pick a preset speaker.';
	if (config.mode === 'voice_design' && !config.instruct.trim()) {
		return 'A designed voice needs a description in Advanced.';
	}
	if (config.instruct.length > 1000) return 'Instruct is limited to 1,000 characters.';
	return null;
}

export function buildSpeakRequest(text: string, config: SpeakConfig): SpeakRequest {
	const request: SpeakRequest = {
		text: text.trim(),
		engine: config.engineId || null,
		model: config.modelAlias || null,
		instruct: '',
		quality: config.quality,
		language: config.language,
		format: config.format,
		speed: config.speed,
		take: config.take,
		seed: config.seed,
		markup: config.markup,
		spell_identifiers: config.spellIdentifiers,
		paragraph_gap_ms: config.paragraphGapMs,
		project: config.project.trim() ? config.project.trim() : null
	};
	if (config.mode === 'clone') request.voice = config.voiceId || null;
	if (config.mode === 'custom_voice') {
		request.speaker = config.speaker || null;
		if (config.instruct.trim()) request.instruct = config.instruct.trim();
	}
	if (config.mode === 'voice_design') request.instruct = config.instruct.trim();
	return request;
}

export function qualityForModel(model: ModelSummary | null): 'high' | 'fast' {
	return model?.qualityTier === 'fast' ? 'fast' : 'high';
}

export function modeSummary(config: SpeakConfig, engines: EngineSummary[]): string {
	const engine = findEngine(engines, config.engineId);
	const model = engine?.models.find((item) => item.alias === config.modelAlias);
	if (!engine || !model) return 'No model selected';
	return `${engine.displayName} · ${model.alias}`;
}
