import {
	ApiError,
	deleteVoice,
	describeApiError,
	getEngines,
	getGpu,
	getHealth,
	getVoices,
	unloadModels
} from '#lib/api/studio.js';
import type {
	ApiErrorPayload,
	ClipSummary,
	EngineSummary,
	GpuStatus,
	HealthSummary,
	SpeakConfig,
	SpeakResult,
	UnloadResult,
	VoiceSummary
} from '#lib/api/types.js';
import {
	configForEngine,
	configForMode,
	createDefaultSpeakConfig,
	defaultSpeakConfig,
	presetForSpeak,
	voiceForSpeak
} from '#lib/speak.js';
import type { BuiltinPreset } from '#lib/voices.js';
import { shortText } from '#lib/format.js';

type Connection = 'checking' | 'connected' | 'offline';

function readRailCollapsed(): boolean {
	if (typeof localStorage === 'undefined') return false;
	return localStorage.getItem('agent-voice-rail-collapsed') === '1';
}

export function clipFromSpeak(result: SpeakResult, text: string): ClipSummary {
	return {
		id: result.artifactId,
		title: shortText(text, 48) || 'Untitled render',
		subtitle: `${result.engineId} · ${result.modelAlias}`,
		url: result.downloadUrl || `/audio/${result.artifactId}`,
		durationSec: result.durationSec,
		engineId: result.engineId,
		modelAlias: result.modelAlias,
		cached: result.cached,
		elapsedMs: result.elapsedMs,
		realtimeMultiple: result.realtimeMultiple,
		format: result.format,
		createdAt: Date.now(),
		voiceId: result.voiceId,
		speaker: result.speaker,
		mode: result.mode
	};
}

class Studio {
	connection = $state<Connection>('checking');
	health = $state<HealthSummary | null>(null);
	gpu = $state<GpuStatus | null>(null);
	gpuError = $state('');
	engines = $state<EngineSummary[]>([]);
	voices = $state<VoiceSummary[]>([]);
	catalogLoading = $state(true);
	catalogError = $state<ApiErrorPayload | null>(null);
	speak = $state<SpeakConfig>(defaultSpeakConfig());
	sessionClips = $state<ClipSummary[]>([]);
	railCollapsed = $state(readRailCollapsed());
	mobileNavOpen = $state(false);
	unloading = $state(false);

	private pollTimer: ReturnType<typeof setInterval> | null = null;

	async refreshConnection(): Promise<void> {
		try {
			this.health = await getHealth();
			this.connection = 'connected';
		} catch {
			this.health = null;
			this.connection = 'offline';
		}
	}

	async refreshGpu(): Promise<void> {
		try {
			this.gpu = await getGpu();
			this.gpuError = '';
		} catch (error) {
			this.gpuError = describeApiError(error).message;
		}
	}

	async refreshCatalog(): Promise<void> {
		this.catalogLoading = true;
		this.catalogError = null;
		try {
			const [engines, voices] = await Promise.all([getEngines(), getVoices()]);
			this.engines = engines;
			this.voices = voices;
			this.ensureSpeakConfig();
		} catch (error) {
			this.catalogError = describeApiError(error);
		} finally {
			this.catalogLoading = false;
		}
	}

	async refreshVoices(): Promise<void> {
		try {
			this.voices = await getVoices();
		} catch (error) {
			this.catalogError = describeApiError(error);
		}
	}

	async refreshEngines(): Promise<void> {
		try {
			this.engines = await getEngines();
		} catch (error) {
			this.catalogError = describeApiError(error);
		}
	}

	startPolling(): void {
		if (this.pollTimer !== null) return;
		void this.refreshConnection();
		void this.refreshGpu();
		void this.refreshCatalog();
		this.pollTimer = setInterval(() => {
			if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
			void this.refreshConnection();
			void this.refreshGpu();
		}, 5000);
	}

	stopPolling(): void {
		if (this.pollTimer !== null) {
			clearInterval(this.pollTimer);
			this.pollTimer = null;
		}
	}

	ensureSpeakConfig(): void {
		const current = this.engines.find((engine) => engine.id === this.speak.engineId);
		const modelStillExists =
			current?.models.some(
				(model) => model.alias === this.speak.modelAlias && model.mode === this.speak.mode
			) ?? false;
		if (!current || !modelStillExists) {
			this.speak = createDefaultSpeakConfig(this.engines, this.voices);
		}
	}

	selectEngine(engineId: string): void {
		const engine = this.engines.find((item) => item.id === engineId);
		if (!engine) return;
		this.speak = configForEngine(this.speak, engine, this.voices);
	}

	selectMode(mode: SpeakConfig['mode']): void {
		const engine = this.engines.find((item) => item.id === this.speak.engineId);
		if (!engine) return;
		this.speak = configForMode(this.speak, engine, mode, this.voices);
	}

	selectModel(alias: string): void {
		const engine = this.engines.find((item) => item.id === this.speak.engineId);
		const model = engine?.models.find((item) => item.alias === alias);
		if (!model) return;
		this.speak = {
			...this.speak,
			mode: model.mode,
			modelAlias: model.alias,
			voiceId: model.mode === 'clone' ? this.speak.voiceId : '',
			speaker:
				model.mode === 'custom_voice'
					? model.presetSpeakers.includes(this.speak.speaker)
						? this.speak.speaker
						: (model.presetSpeakers[0] ?? '')
					: ''
		};
	}

	toggleRail(): void {
		this.railCollapsed = !this.railCollapsed;
		try {
			localStorage.setItem('agent-voice-rail-collapsed', this.railCollapsed ? '1' : '0');
		} catch {
			return;
		}
	}

	openMobile(): void {
		this.mobileNavOpen = true;
	}

	closeMobile(): void {
		this.mobileNavOpen = false;
	}

	addClip(result: SpeakResult, text: string): ClipSummary {
		const clip = clipFromSpeak(result, text);
		this.sessionClips = [clip, ...this.sessionClips].slice(0, 50);
		return clip;
	}

	clearSession(): void {
		this.sessionClips = [];
	}

	useVoice(voice: VoiceSummary): void {
		this.speak = voiceForSpeak(this.speak, voice, this.engines);
	}

	usePreset(preset: BuiltinPreset): void {
		const engine = this.engines.find((item) => item.id === preset.engineId);
		const model = engine?.models.find((item) => item.alias === preset.modelAlias);
		if (!engine || !model) return;
		this.speak = presetForSpeak(this.speak, engine, model, preset.speaker);
	}

	async removeVoice(voiceId: string): Promise<void> {
		await deleteVoice(voiceId);
		if (this.speak.voiceId === voiceId) this.speak = { ...this.speak, voiceId: '' };
		await this.refreshVoices();
	}

	async unloadAll(): Promise<UnloadResult> {
		this.unloading = true;
		try {
			const result = await unloadModels();
			await Promise.all([this.refreshGpu(), this.refreshEngines()]);
			return result;
		} catch (error) {
			if (error instanceof ApiError) throw error;
			throw new Error(describeApiError(error).message, { cause: error });
		} finally {
			this.unloading = false;
		}
	}
}

export const studio = new Studio();
