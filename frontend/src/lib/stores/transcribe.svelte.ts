/**
 * Transcribe workspace: upload/record → capture, timed transcript, server-side
 * exports and the capture → voice-draft hand-off.
 */
import { describeApiError } from '#lib/api/client.js';
import {
	captureExportText,
	exportCapture,
	getCapture,
	listCaptures,
	patchCapture,
	transcribeUpload,
	type CaptureExportFormat
} from '#lib/api/captures.js';
import { getModels } from '#lib/api/studio.js';
import type {
	ApiErrorPayload,
	CaptureDetail,
	CaptureSummary,
	EngineSummary,
	ModelSummary,
	TranscribeResult
} from '#lib/api/types.js';
import type { UploadTask } from '#lib/api/uploads.js';
import { clipFromCapture } from '#lib/clips.js';
import { downloadBlob, sanitizeFilename } from '#lib/format.js';
import { clone } from '#lib/stores/clone.svelte.js';
import { getPlayer } from '#lib/stores/player.svelte.js';
import { studio } from '#lib/stores/studio.svelte.js';
import { toasts } from '#lib/stores/toasts.svelte.js';

class TranscribeStore {
	captures = $state<CaptureSummary[]>([]);
	listLoading = $state(true);
	listError = $state<ApiErrorPayload | null>(null);

	selectedId = $state<string | null>(null);
	detail = $state<CaptureDetail | null>(null);
	detailLoading = $state(false);
	detailError = $state<ApiErrorPayload | null>(null);

	engineId = $state('');
	modelAlias = $state('');
	language = $state('auto');
	hotwords = $state('');

	models = $state<ModelSummary[]>([]);
	modelsLoading = $state(false);

	uploading = $state(false);
	uploadRatio = $state(0);
	uploadError = $state<ApiErrorPayload | null>(null);
	lastFileName = $state('');

	saving = $state(false);
	saveError = $state<ApiErrorPayload | null>(null);
	dirty = $state(false);
	exporting = $state<CaptureExportFormat | null>(null);

	private task: UploadTask<TranscribeResult> | null = null;
	private initialised = false;

	get asrEngines(): EngineSummary[] {
		return studio.asrEngines;
	}

	get selectedEngine(): EngineSummary | null {
		return this.asrEngines.find((engine) => engine.id === this.engineId) ?? null;
	}

	get canTranscribe(): boolean {
		return this.engineId !== '' && this.modelAlias !== '' && !this.uploading;
	}

	async init(preferredCaptureId: string | null = null): Promise<void> {
		if (this.initialised) {
			if (preferredCaptureId && preferredCaptureId !== this.selectedId) {
				await this.select(preferredCaptureId);
			}
			return;
		}
		this.initialised = true;
		this.pickEngine();
		await Promise.all([this.loadCaptures(), this.loadModels()]);
		const wanted =
			(preferredCaptureId && this.captures.some((capture) => capture.id === preferredCaptureId)
				? preferredCaptureId
				: null) ??
			this.selectedId ??
			this.captures[0]?.id ??
			null;
		if (wanted) await this.select(wanted);
	}

	async loadModels(): Promise<void> {
		if (!this.engineId) return;
		this.modelsLoading = true;
		try {
			this.models = await getModels(this.engineId);
			if (!this.models.some((model) => model.alias === this.modelAlias)) {
				const preferred =
					this.models.find((model) => model.downloaded && model.isDefault) ??
					this.models.find((model) => model.downloaded) ??
					this.models[0];
				this.modelAlias = preferred?.alias ?? '';
			}
		} catch (error) {
			this.listError = describeApiError(error);
		} finally {
			this.modelsLoading = false;
		}
	}

	pickEngine(): void {
		if (this.engineId && this.asrEngines.some((engine) => engine.id === this.engineId)) return;
		const installed = this.asrEngines.find((engine) => engine.installed) ?? this.asrEngines[0];
		this.engineId = installed?.id ?? '';
		const languages = installed?.languages ?? [];
		this.language = languages.includes('English') ? 'en' : languages[0] ? 'auto' : 'auto';
		void this.loadModels();
	}

	async selectEngine(engineId: string): Promise<void> {
		this.engineId = engineId;
		this.modelAlias = '';
		await this.loadModels();
	}

	async loadCaptures(): Promise<void> {
		this.listLoading = true;
		this.listError = null;
		try {
			const page = await listCaptures({ limit: 100 });
			this.captures = page.items;
		} catch (error) {
			this.listError = describeApiError(error);
		} finally {
			this.listLoading = false;
		}
	}

	async select(captureId: string): Promise<void> {
		this.selectedId = captureId;
		this.detailLoading = true;
		this.detailError = null;
		this.dirty = false;
		try {
			this.detail = await getCapture(captureId);
		} catch (error) {
			this.detailError = describeApiError(error);
			this.detail = null;
		} finally {
			this.detailLoading = false;
		}
	}

	async transcribe(file: File): Promise<boolean> {
		if (!this.canTranscribe) {
			this.uploadError = {
				status: 0,
				code: 'NO_ENGINE',
				message: 'No ASR engine is installed.',
				suggestion: 'Install one on Models, then reload this screen.',
				correlationId: ''
			};
			return false;
		}
		this.uploading = true;
		this.uploadRatio = 0;
		this.uploadError = null;
		this.lastFileName = file.name;
		this.task = transcribeUpload({
			file,
			model: this.modelAlias,
			language: this.language,
			hotwords: this.hotwords,
			onProgress: (ratio) => {
				this.uploadRatio = ratio;
			}
		});
		try {
			const result = await this.task.promise;
			await this.loadCaptures();
			if (result.captureId) {
				await this.select(result.captureId);
			} else if (this.captures[0]) {
				await this.select(this.captures[0].id);
			}
			toasts.success('Transcription ready', `${result.segmentsCount} segments`);
			return true;
		} catch (error) {
			this.uploadError = describeApiError(error);
			return false;
		} finally {
			this.uploading = false;
			this.task = null;
		}
	}

	async saveTranscript(text: string, title?: string): Promise<void> {
		if (!this.detail) return;
		this.saving = true;
		this.saveError = null;
		try {
			const updated = await patchCapture(this.detail.id, {
				transcript: text,
				...(title !== undefined ? { title } : {})
			});
			this.detail = updated;
			this.captures = this.captures.map((capture) =>
				capture.id === updated.id
					? { ...capture, transcript: updated.transcript, title: updated.title }
					: capture
			);
			this.dirty = false;
			toasts.success('Transcript saved', 'The capture was updated on disk.');
		} catch (error) {
			this.saveError = describeApiError(error);
		} finally {
			this.saving = false;
		}
	}

	markDirty(): void {
		this.dirty = true;
	}

	async exportAs(format: CaptureExportFormat): Promise<void> {
		if (!this.detail) return;
		this.exporting = format;
		try {
			const blob = await exportCapture(this.detail.id, format);
			const name = sanitizeFilename(this.detail.title || this.detail.id);
			downloadBlob(`${name}.${format}`, blob);
		} catch (error) {
			toasts.error('Export failed', describeApiError(error).message);
		} finally {
			this.exporting = null;
		}
	}

	async copyText(format: CaptureExportFormat): Promise<void> {
		if (!this.detail) return;
		try {
			const text = await captureExportText(this.detail.id, format);
			await navigator.clipboard.writeText(text);
			toasts.success('Copied', `${format.toUpperCase()} written to the clipboard.`);
		} catch (error) {
			toasts.error('Could not copy', describeApiError(error).message);
		}
	}

	async useAsSample(): Promise<void> {
		if (!this.detail) return;
		await clone.beginFromCapture(this.detail);
	}

	playCapture(
		capture: CaptureSummary = this.detail ?? this.captures[0] ?? ({} as CaptureSummary)
	): void {
		if (!capture.id) return;
		void getPlayer().play(clipFromCapture(capture));
	}

	async remove(captureId: string): Promise<void> {
		const { deleteCapture } = await import('#lib/api/captures.js');
		try {
			await deleteCapture(captureId);
			this.captures = this.captures.filter((capture) => capture.id !== captureId);
			if (this.selectedId === captureId) {
				this.selectedId = null;
				this.detail = null;
				const next = this.captures[0];
				if (next) await this.select(next.id);
			}
		} catch (error) {
			toasts.error('Could not delete the capture', describeApiError(error).message);
		}
	}
}

export const transcribe = new TranscribeStore();
