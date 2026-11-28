/**
 * Catalog and liveness: health, GPU, engines, voices and presets.
 *
 * Polling lives here (started by the shell) so no page owns a timer.
 */
import { describeApiError, ApiError } from '#lib/api/client.js';
import { listPresets } from '#lib/api/presets.js';
import { getEngines, getGpu, getHealth, unloadModels } from '#lib/api/studio.js';
import { listVoices } from '#lib/api/voices.js';
import type {
	ApiErrorPayload,
	EngineSummary,
	GpuStatus,
	HealthState,
	HealthSummary,
	PresetVoice,
	UnloadResult,
	VoiceSummary
} from '#lib/api/types.js';

const POLL_MS = 5000;

class StudioCatalog {
	connection = $state<HealthState>('checking');
	health = $state<HealthSummary | null>(null);
	gpu = $state<GpuStatus | null>(null);
	gpuError = $state('');
	engines = $state<EngineSummary[]>([]);
	voices = $state<VoiceSummary[]>([]);
	presets = $state<PresetVoice[]>([]);
	catalogLoading = $state(true);
	catalogError = $state<ApiErrorPayload | null>(null);
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
			const [engines, voices, presets] = await Promise.all([
				getEngines(),
				listVoices(),
				listPresets()
			]);
			this.engines = engines;
			this.voices = voices;
			this.presets = presets;
		} catch (error) {
			this.catalogError = describeApiError(error);
		} finally {
			this.catalogLoading = false;
		}
	}

	async refreshVoices(): Promise<void> {
		try {
			this.voices = await listVoices();
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

	async refreshPresets(): Promise<void> {
		try {
			this.presets = await listPresets();
		} catch (error) {
			this.catalogError = describeApiError(error);
		}
	}

	startPolling(): void {
		if (this.pollTimer !== null) return;
		void this.refreshConnection();
		void this.refreshGpu();
		this.pollTimer = setInterval(() => {
			if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
			void this.refreshConnection();
			void this.refreshGpu();
		}, POLL_MS);
	}

	stopPolling(): void {
		if (this.pollTimer !== null) {
			clearInterval(this.pollTimer);
			this.pollTimer = null;
		}
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

	get ttsEngines(): EngineSummary[] {
		return this.engines.filter((engine) => engine.kind === 'tts');
	}

	get asrEngines(): EngineSummary[] {
		return this.engines.filter((engine) => engine.kind === 'asr');
	}
}

export const studio = new StudioCatalog();
