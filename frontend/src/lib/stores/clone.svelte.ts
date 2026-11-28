/**
 * Clone voice flow: sample → review/trim → name & save.
 *
 * Every upload goes through `POST /v1/voice-drafts` with real progress; the
 * client only ever sends `draft_id` (never a server path) and discards an
 * abandoned draft when the dialog closes.
 */
import { describeApiError } from '#lib/api/client.js';
import { captureToDraft } from '#lib/api/captures.js';
import { deleteDraft, uploadDraft } from '#lib/api/drafts.js';
import { previewVoice } from '#lib/api/voices.js';
import { addVoiceSample, createVoiceFromDraft } from '#lib/api/voices.js';
import type { ApiErrorPayload, CaptureSummary, VoiceDraft, VoiceSummary } from '#lib/api/types.js';
import type { UploadTask } from '#lib/api/uploads.js';
import { clipFromPreview } from '#lib/clips.js';
import { getPlayer } from '#lib/stores/player.svelte.js';
import { studio } from '#lib/stores/studio.svelte.js';
import { toasts } from '#lib/stores/toasts.svelte.js';

export type CloneStep = 'sample' | 'review' | 'save';
export type CloneMode = 'create' | 'add-sample';

export const MIN_TRIM_SECONDS = 0.5;
const ACCEPTED_AUDIO = /\.(wav|mp3|flac|ogg|oga|opus|m4a|aac|webm|mp4|mov|mkv)$/i;

function languageOptions(): string[] {
	const languages: string[] = ['Auto'];
	for (const engine of studio.engines) {
		if (engine.kind !== 'tts') continue;
		for (const language of engine.languages) {
			if (language !== 'Auto' && !languages.includes(language)) languages.push(language);
		}
	}
	return languages;
}

class CloneStore {
	open = $state(false);
	mode = $state<CloneMode>('create');
	step = $state<CloneStep>('sample');
	target = $state<VoiceSummary | null>(null);
	fileName = $state('');
	uploading = $state(false);
	uploadRatio = $state(0);
	draft = $state<VoiceDraft | null>(null);
	error = $state<ApiErrorPayload | null>(null);
	trimStart = $state(0);
	trimEnd = $state(0);
	transcript = $state('');
	xVectorOnly = $state(false);
	name = $state('');
	language = $state('Auto');
	notes = $state('');
	consent = $state(false);
	makePrimary = $state(false);
	saving = $state(false);
	saveError = $state<ApiErrorPayload | null>(null);
	savedVoiceId = $state<string | null>(null);
	previewing = $state(false);

	private task: UploadTask<VoiceDraft> | null = null;

	get duration(): number {
		return this.draft?.durationSec ?? 0;
	}

	get trimLength(): number {
		return Math.max(0, this.trimEnd - this.trimStart);
	}

	get trimValid(): boolean {
		if (!this.draft) return false;
		return (
			this.trimStart >= 0 &&
			this.trimEnd <= this.duration + 0.001 &&
			this.trimEnd - this.trimStart >= MIN_TRIM_SECONDS
		);
	}

	get canContinue(): boolean {
		return this.draft !== null && this.trimValid;
	}

	get canSave(): boolean {
		return (
			this.draft !== null &&
			this.trimValid &&
			this.name.trim().length > 0 &&
			this.name.trim().length <= 64 &&
			this.consent &&
			!this.saving
		);
	}

	get languages(): string[] {
		return languageOptions();
	}

	begin(mode: CloneMode, target: VoiceSummary | null = null): void {
		this.reset();
		this.mode = mode;
		this.target = target;
		this.step = 'sample';
		this.open = true;
		if (target) this.name = target.name;
	}

	async close(): Promise<void> {
		const draft = this.draft;
		const saved = this.savedVoiceId !== null;
		this.reset();
		this.open = false;
		if (draft && !saved) {
			try {
				await deleteDraft(draft.draftId);
			} catch {
				// An expired draft is already gone; nothing else to clean up.
			}
		}
	}

	reset(): void {
		this.task?.abort();
		this.task = null;
		this.mode = 'create';
		this.step = 'sample';
		this.target = null;
		this.fileName = '';
		this.uploading = false;
		this.uploadRatio = 0;
		this.draft = null;
		this.error = null;
		this.trimStart = 0;
		this.trimEnd = 0;
		this.transcript = '';
		this.xVectorOnly = false;
		this.name = '';
		this.language = 'Auto';
		this.notes = '';
		this.consent = false;
		this.makePrimary = false;
		this.saving = false;
		this.saveError = null;
		this.savedVoiceId = null;
		this.previewing = false;
	}

	async acceptFile(file: File): Promise<void> {
		if (file.size === 0) {
			this.error = {
				status: 0,
				code: 'EMPTY_FILE',
				message: 'That file is empty.',
				suggestion: 'Choose an audio file with sound in it.',
				correlationId: ''
			};
			return;
		}
		if (!ACCEPTED_AUDIO.test(file.name) && !file.type.startsWith('audio/')) {
			this.error = {
				status: 0,
				code: 'UNSUPPORTED_FILE',
				message: `“${file.name}” is not an audio file the daemon accepts.`,
				suggestion: 'Use WAV, FLAC, Ogg/Opus, MP3, M4A or a webm/ogg recording.',
				correlationId: ''
			};
			return;
		}
		this.fileName = file.name;
		this.uploading = true;
		this.uploadRatio = 0;
		this.error = null;
		this.draft = null;
		this.task = uploadDraft(file, {
			onProgress: (ratio) => {
				this.uploadRatio = ratio;
			}
		});
		try {
			const draft = await this.task.promise;
			this.draft = draft;
			this.trimStart = 0;
			this.trimEnd = draft.durationSec;
			this.transcript = draft.suggestedTranscript ?? '';
			if (draft.suggestedTranscriptLanguage) {
				this.language = draft.suggestedTranscriptLanguage;
			}
			if (draft.suggestedTranscriptError) {
				toasts.info(
					'The transcript could not be generated',
					draft.suggestedTranscriptError.message
				);
			}
			if (draft.suggestedTranscript === null && draft.suggestedTranscriptError === null) {
				toasts.info('Type the reference text', 'A transcript improves clone quality.');
			}
		} catch (error) {
			this.error = describeApiError(error);
			this.fileName = '';
		} finally {
			this.uploading = false;
			this.task = null;
		}
	}

	async acceptRecording(blob: Blob): Promise<void> {
		const type = blob.type || 'audio/webm';
		const extension = type.includes('ogg') ? 'ogg' : type.includes('mp4') ? 'm4a' : 'webm';
		const file = new File([blob], `recording-${Date.now()}.${extension}`, { type });
		await this.acceptFile(file);
	}

	setTrim(start: number, end: number): void {
		const duration = this.duration;
		if (duration <= 0) return;
		const nextStart = Math.max(0, Math.min(start, duration - MIN_TRIM_SECONDS));
		const nextEnd = Math.min(duration, Math.max(end, nextStart + MIN_TRIM_SECONDS));
		this.trimStart = Number(nextStart.toFixed(3));
		this.trimEnd = Number(nextEnd.toFixed(3));
	}

	resetTrim(): void {
		this.trimStart = 0;
		this.trimEnd = this.duration;
	}

	async beginFromCapture(capture: CaptureSummary): Promise<void> {
		this.begin('create');
		this.fileName = capture.title || 'Capture';
		this.uploading = true;
		this.error = null;
		try {
			const draft = await captureToDraft(capture.id);
			this.draft = draft;
			this.trimStart = 0;
			this.trimEnd = draft.durationSec;
			this.transcript = draft.suggestedTranscript ?? '';
			this.step = 'review';
			this.name = capture.title ? `${capture.title} voice` : '';
		} catch (error) {
			this.error = describeApiError(error);
		} finally {
			this.uploading = false;
		}
	}

	goTo(step: CloneStep): void {
		if (step === 'review' && !this.draft) return;
		if (step === 'save' && !this.canContinue) return;
		this.step = step;
	}

	async save(): Promise<boolean> {
		if (!this.draft || !this.canSave) return false;
		this.saving = true;
		this.saveError = null;
		const trim = { startSec: this.trimStart, endSec: this.trimEnd };
		try {
			if (this.mode === 'add-sample' && this.target) {
				await addVoiceSample({
					voiceId: this.target.id,
					draftId: this.draft.draftId,
					trim,
					refText: this.transcript.trim(),
					xVectorOnly: this.xVectorOnly,
					makePrimary: this.makePrimary,
					consent: this.consent
				});
				this.savedVoiceId = this.target.id;
				toasts.success('Sample added', `“${this.target.name}” now has another reference.`);
			} else {
				const created = await createVoiceFromDraft({
					name: this.name.trim(),
					draftId: this.draft.draftId,
					trim,
					refText: this.transcript.trim(),
					xVectorOnly: this.xVectorOnly,
					language: this.language,
					notes: this.notes.trim(),
					consent: this.consent
				});
				this.savedVoiceId = created.id;
				toasts.success('Voice saved', `“${created.name}” is in your library.`);
			}
			await studio.refreshVoices();
			return true;
		} catch (error) {
			this.saveError = describeApiError(error);
			return false;
		} finally {
			this.saving = false;
		}
	}

	async tryIt(): Promise<void> {
		if (!this.savedVoiceId) return;
		this.previewing = true;
		try {
			const preview = await previewVoice(this.savedVoiceId, 'This is how the saved voice sounds.');
			const name =
				this.mode === 'add-sample' && this.target ? this.target.name : this.name.trim() || 'Voice';
			void getPlayer().play(clipFromPreview(preview, name));
		} catch (error) {
			this.saveError = describeApiError(error);
		} finally {
			this.previewing = false;
		}
	}
}

export const clone = new CloneStore();
