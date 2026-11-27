import type {
	EngineSummary,
	ModelMode,
	ModelSummary,
	SpeakConfig,
	SpeakRequest,
	VoiceSummary
} from '#lib/api/types.js';

export const MODE_LABELS: Record<ModelMode, string> = {
	clone: 'Clone Voice',
	custom_voice: 'Preset Speaker',
	voice_design: 'Voice Design'
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

export function pickSpeaker(model: ModelSummary | null): string {
	return model?.presetSpeakers[0] ?? '';
}

export function defaultMode(engine: EngineSummary, voices: VoiceSummary[]): ModelMode {
	const modes = modesForEngine(engine);
	if (modes.includes('clone') && voices.length > 0) return 'clone';
	if (modes.includes('custom_voice')) return 'custom_voice';
	if (modes.includes('voice_design')) return 'voice_design';
	return modes[0] ?? 'clone';
}

export function defaultSpeakConfig(): SpeakConfig {
	return {
		mode: 'clone',
		engineId: '',
		modelAlias: '',
		voiceId: '',
		speaker: '',
		instruct: '',
		language: 'Auto',
		format: 'wav',
		speed: 1,
		take: null,
		seed: null,
		markup: true,
		spellIdentifiers: false,
		paragraphGapMs: 0
	};
}

export function createDefaultSpeakConfig(
	engines: EngineSummary[],
	voices: VoiceSummary[]
): SpeakConfig {
	const config = defaultSpeakConfig();
	const available = ttsEngines(engines);
	const engine =
		available.find((item) => item.installed) ??
		available.find((item) => item.models.length > 0) ??
		available[0] ??
		null;
	if (!engine) return config;
	return configForEngine(config, engine, voices);
}

export function configForEngine(
	config: SpeakConfig,
	engine: EngineSummary,
	voices: VoiceSummary[]
): SpeakConfig {
	const modes = modesForEngine(engine);
	const requestedModeUsable =
		modes.includes(config.mode) && !(config.mode === 'clone' && voices.length === 0);
	const mode = requestedModeUsable ? config.mode : defaultMode(engine, voices);
	const model = pickModel(engine, mode);
	const voice = mode === 'clone' ? (voices[0]?.id ?? '') : '';
	const speaker = mode === 'custom_voice' ? pickSpeaker(model) : '';
	return {
		...config,
		engineId: engine.id,
		mode,
		modelAlias: model?.alias ?? '',
		voiceId: voice,
		speaker,
		instruct: mode === 'clone' ? '' : config.instruct,
		language: languageFor(engine, config.language)
	};
}

export function configForMode(
	config: SpeakConfig,
	engine: EngineSummary,
	mode: ModelMode,
	voices: VoiceSummary[]
): SpeakConfig {
	const model = pickModel(engine, mode);
	return {
		...config,
		mode,
		modelAlias: model?.alias ?? '',
		voiceId: mode === 'clone' ? (voices[0]?.id ?? '') : '',
		speaker: mode === 'custom_voice' ? pickSpeaker(model) : '',
		instruct: mode === 'clone' ? '' : config.instruct
	};
}

export function languageFor(engine: EngineSummary | null, current: string): string {
	if (!engine || engine.languages.length === 0) return current || 'Auto';
	return engine.languages.includes(current) ? current : (engine.languages[0] ?? 'Auto');
}

export function voiceForSpeak(
	config: SpeakConfig,
	voice: VoiceSummary,
	engines: EngineSummary[]
): SpeakConfig {
	const candidates = ttsEngines(engines).filter((engine) =>
		engine.models.some((model) => model.mode === 'clone')
	);
	const current = findEngine(engines, config.engineId);
	const engine =
		current && current.models.some((model) => model.mode === 'clone')
			? current
			: (candidates.find((item) => item.installed) ?? candidates[0] ?? null);
	if (!engine) return { ...config, mode: 'clone', voiceId: voice.id, speaker: '' };
	const model = pickModel(engine, 'clone');
	return {
		...config,
		mode: 'clone',
		engineId: engine.id,
		modelAlias: model?.alias ?? '',
		voiceId: voice.id,
		speaker: '',
		instruct: '',
		language: languageFor(engine, config.language)
	};
}

export function presetForSpeak(
	config: SpeakConfig,
	engine: EngineSummary,
	model: ModelSummary,
	speaker: string
): SpeakConfig {
	return {
		...config,
		mode: 'custom_voice',
		engineId: engine.id,
		modelAlias: model.alias,
		voiceId: '',
		speaker: speaker || pickSpeaker(model),
		language: languageFor(engine, config.language)
	};
}

export function validateSpeak(text: string, config: SpeakConfig): string | null {
	if (!text.trim()) return 'Write a line before generating.';
	if (text.length > 20000) return 'The daemon accepts at most 20,000 characters.';
	if (!config.engineId) return 'Choose an engine.';
	if (!config.modelAlias) return 'Choose a model.';
	if (config.mode === 'clone' && !config.voiceId) {
		return 'Clone mode needs a registered voice. Add one in Voices, or switch to preset/design.';
	}
	if (config.mode === 'custom_voice' && !config.speaker) {
		return 'Preset mode needs a speaker.';
	}
	if (config.mode === 'voice_design' && !config.instruct.trim()) {
		return 'Voice Design needs a description.';
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
		quality: 'high',
		language: config.language,
		format: config.format,
		speed: config.speed,
		take: config.take,
		seed: config.seed,
		markup: config.markup,
		spell_identifiers: config.spellIdentifiers,
		paragraph_gap_ms: config.paragraphGapMs
	};
	if (config.mode === 'clone') {
		request.voice = config.voiceId || null;
	}
	if (config.mode === 'custom_voice') {
		request.speaker = config.speaker || null;
		if (config.instruct.trim()) request.instruct = config.instruct.trim();
	}
	if (config.mode === 'voice_design') {
		request.instruct = config.instruct.trim();
	}
	return request;
}

export function qualityForModel(model: ModelSummary | null): 'high' | 'fast' {
	return model?.qualityTier === 'fast' ? 'fast' : 'high';
}
