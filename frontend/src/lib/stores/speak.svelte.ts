/**
 * Speak workspace state: script, voice-first selection, job-based generation and
 * the persistent take list (history, not session state).
 */
import { describeApiError } from '#lib/api/client.js';
import {
	deleteHistoryItem,
	favoriteHistoryItem,
	listHistory,
	rerenderHistoryItem
} from '#lib/api/history.js';
import { getJob, cancelJob } from '#lib/api/jobs.js';
import { startSpeakJob, synthesisFromJob } from '#lib/api/speak.js';
import type {
	ApiErrorPayload,
	HistoryItem,
	Job,
	ModelMode,
	PresetVoice,
	SpeakConfig,
	SynthesisResult,
	VoiceSummary
} from '#lib/api/types.js';
import { clipFromHistory, clipFromSynthesis } from '#lib/clips.js';
import {
	autoConfig,
	buildSpeakRequest,
	configForEngine,
	configForModel,
	configForPreset,
	configForVoice,
	defaultSpeakConfig,
	ensureConfig,
	validateSpeak
} from '#lib/speak.js';
import { events, jobFromEvent } from '#lib/stores/events.svelte.js';
import { getPlayer } from '#lib/stores/player.svelte.js';
import { studio } from '#lib/stores/studio.svelte.js';
import { toasts } from '#lib/stores/toasts.svelte.js';

export type SpeakPhase =
	'idle' | 'queued' | 'loading' | 'generating' | 'done' | 'failed' | 'cancelled';

const SAMPLE_SCRIPT = 'The first take is in. A quiet room, a measured pace, and one line to keep.';

class SpeakStore {
	text = $state(SAMPLE_SCRIPT);
	config = $state<SpeakConfig>(defaultSpeakConfig());

	phase = $state<SpeakPhase>('idle');
	jobId = $state<string | null>(null);
	jobMessage = $state('');
	jobProgress = $state(0);
	elapsedMs = $state(0);
	error = $state<ApiErrorPayload | null>(null);
	result = $state<SynthesisResult | null>(null);

	takes = $state<HistoryItem[]>([]);
	takesLoading = $state(false);
	takesError = $state('');
	compareLeft = $state<string | null>(null);
	compareRight = $state<string | null>(null);

	private unsubscribe: (() => void) | null = null;
	private ticker: ReturnType<typeof setInterval> | null = null;
	private poller: ReturnType<typeof setInterval> | null = null;
	private startedAt = 0;
	private initialised = false;

	get busy(): boolean {
		return this.phase === 'queued' || this.phase === 'loading' || this.phase === 'generating';
	}

	get validationError(): string | null {
		return validateSpeak(this.text, this.config);
	}

	get characterCount(): number {
		return this.text.length;
	}

	get wordCount(): number {
		const trimmed = this.text.trim();
		return trimmed ? trimmed.split(/\s+/).length : 0;
	}

	init(): void {
		if (this.initialised) return;
		this.initialised = true;
		if (!this.config.engineId) {
			this.config = autoConfig(studio.engines, studio.voices, studio.presets);
		}
		this.unsubscribe = events.subscribe((message) => {
			const job = jobFromEvent(message);
			if (job && this.jobId && job.jobId === this.jobId) this.applyJob(job);
		});
		void this.loadTakes();
	}

	dispose(): void {
		// Stop listening while the page is away; the poll timer keeps a running job
		// observable so a finished take still lands in history.
		this.unsubscribe?.();
		this.unsubscribe = null;
		this.initialised = false;
	}

	syncCatalog(): void {
		this.config = ensureConfig(this.config, studio.engines, studio.voices, studio.presets);
	}

	patch(patch: Partial<SpeakConfig>): void {
		this.config = { ...this.config, ...patch };
	}

	selectVoice(voice: VoiceSummary): void {
		this.config = configForVoice(this.config, voice, studio.engines);
	}

	selectPreset(preset: PresetVoice): void {
		this.config = configForPreset(this.config, preset, studio.engines);
	}

	selectEngine(engineId: string): void {
		const engine = studio.engines.find((item) => item.id === engineId);
		if (!engine) return;
		this.config = configForEngine(this.config, engine, studio.voices, studio.presets);
	}

	selectMode(mode: ModelMode): void {
		const engine = studio.engines.find((item) => item.id === this.config.engineId);
		if (!engine) return;
		const model = engine.models.find((item) => item.mode === mode);
		if (!model) return;
		this.config = {
			...configForModel(this.config, model),
			mode
		};
	}

	selectModel(alias: string): void {
		const engine = studio.engines.find((item) => item.id === this.config.engineId);
		const model = engine?.models.find((item) => item.alias === alias);
		if (!model) return;
		this.config = configForModel(this.config, model);
	}

	async generate(): Promise<boolean> {
		if (this.busy) return false;
		const problem = this.validationError;
		if (problem) {
			this.error = {
				status: 0,
				code: 'VALIDATION',
				message: problem,
				suggestion: '',
				correlationId: ''
			};
			this.phase = 'failed';
			return false;
		}
		this.error = null;
		this.result = null;
		this.phase = 'queued';
		this.jobMessage = 'Queued';
		this.jobProgress = 0;
		this.elapsedMs = 0;
		const request = buildSpeakRequest(this.text, this.config);
		try {
			const started = await startSpeakJob(request);
			this.jobId = started.jobId;
			this.startedAt = Date.now();
			this.startTimers(started.jobId);
			return true;
		} catch (error) {
			this.fail(error);
			return false;
		}
	}

	async checkJob(): Promise<void> {
		if (!this.jobId || !this.busy) return;
		try {
			const job = await getJob(this.jobId);
			this.applyJob(job);
		} catch {
			// A transient poll failure must not kill a live job; SSE may still land.
		}
	}

	async cancel(): Promise<void> {
		if (!this.jobId) return;
		try {
			await cancelJob(this.jobId);
			this.stopTimers();
			this.phase = 'cancelled';
			this.jobMessage = 'Cancelled';
		} catch (error) {
			toasts.error('Could not cancel the render', describeApiError(error).message);
		}
	}

	private applyJob(job: Job): void {
		this.jobMessage = job.message;
		this.jobProgress = job.progress;
		switch (job.status) {
			case 'pending':
				this.phase = 'queued';
				break;
			case 'running':
				this.phase = job.progress > 0 ? 'generating' : 'loading';
				break;
			case 'succeeded':
				this.finish(job);
				break;
			case 'failed':
				this.stopTimers();
				this.phase = 'failed';
				this.error = job.error ?? {
					status: 0,
					code: 'JOB_FAILED',
					message: job.message || 'The render failed.',
					suggestion: '',
					correlationId: ''
				};
				break;
			case 'cancelled':
				this.stopTimers();
				this.phase = 'cancelled';
				break;
			default:
				break;
		}
	}

	private finish(job: Job): void {
		this.stopTimers();
		try {
			const result = synthesisFromJob(job);
			this.result = result;
			this.phase = 'done';
			this.jobProgress = 1;
			const clip = clipFromSynthesis(result, this.text);
			void getPlayer().play(clip);
			toasts.success('Take ready', `${clip.title} · ${clip.durationSec.toFixed(2)}s`);
			void this.loadTakes();
		} catch (error) {
			this.phase = 'failed';
			this.error = describeApiError(error);
		}
	}

	private fail(error: unknown): void {
		this.stopTimers();
		this.phase = 'failed';
		this.error = describeApiError(error);
	}

	private startTimers(jobId: string): void {
		this.stopTimers();
		this.ticker = setInterval(() => {
			this.elapsedMs = Date.now() - this.startedAt;
		}, 250);
		this.poller = setInterval(() => {
			if (this.jobId !== jobId) return;
			void this.checkJob();
		}, 1200);
	}

	private stopTimers(): void {
		if (this.ticker !== null) clearInterval(this.ticker);
		if (this.poller !== null) clearInterval(this.poller);
		this.ticker = null;
		this.poller = null;
	}

	resetGeneration(): void {
		this.stopTimers();
		this.phase = 'idle';
		this.jobId = null;
		this.error = null;
		this.jobMessage = '';
		this.jobProgress = 0;
		this.elapsedMs = 0;
	}

	async loadTakes(): Promise<void> {
		this.takesLoading = true;
		this.takesError = '';
		try {
			const page = await listHistory({ limit: 25, kind: 'speak' });
			this.takes = page.items;
		} catch (error) {
			this.takesError = describeApiError(error).message;
		} finally {
			this.takesLoading = false;
		}
	}

	async toggleFavorite(item: HistoryItem): Promise<void> {
		try {
			const updated = await favoriteHistoryItem(item.id);
			this.replaceTake(updated);
		} catch (error) {
			toasts.error('Could not update the favourite', describeApiError(error).message);
		}
	}

	async deleteTake(item: HistoryItem): Promise<void> {
		try {
			await deleteHistoryItem(item.id);
			this.takes = this.takes.filter((entry) => entry.id !== item.id);
			if (this.compareLeft === item.id) this.compareLeft = null;
			if (this.compareRight === item.id) this.compareRight = null;
		} catch (error) {
			toasts.error('Could not delete the take', describeApiError(error).message);
		}
	}

	async rerenderTake(item: HistoryItem, options: { take?: number | null } = {}): Promise<void> {
		if (!item.rerenderable) return;
		try {
			const started = await rerenderHistoryItem(item.id, {
				take: options.take ?? (item.take ?? 1) + 1,
				seed: null
			});
			this.jobId = started.jobId;
			this.phase = 'queued';
			this.jobMessage = 'Rerendering';
			this.startedAt = Date.now();
			this.startTimers(started.jobId);
			toasts.info('Rerender queued', 'The new take appears in this list when it lands.');
		} catch (error) {
			toasts.error('Could not rerender', describeApiError(error).message);
		}
	}

	playTake(item: HistoryItem): void {
		const clip = clipFromHistory(item);
		if (!clip) {
			toasts.info('No audio for this entry', 'This history item has no stored artifact.');
			return;
		}
		void getPlayer().play(clip);
	}

	setCompare(side: 'left' | 'right', id: string | null): void {
		if (side === 'left') this.compareLeft = this.compareLeft === id ? null : id;
		else this.compareRight = this.compareRight === id ? null : id;
	}

	private replaceTake(updated: HistoryItem): void {
		this.takes = this.takes.map((item) => (item.id === updated.id ? updated : item));
	}
}

export const speak = new SpeakStore();
