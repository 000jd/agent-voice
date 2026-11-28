<script lang="ts">
	import { onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import Play from '@lucide/svelte/icons/play';
	import Square from '@lucide/svelte/icons/square';
	import AudioRecorder from '#lib/components/common/audio-recorder.svelte';
	import Dialog from '#lib/components/common/dialog.svelte';
	import Dropzone from '#lib/components/common/dropzone.svelte';
	import ErrorNotice from '#lib/components/common/error-notice.svelte';
	import TrimWaveform from '#lib/components/common/trim-waveform.svelte';
	import { requestBlob } from '#lib/api/client.js';
	import { draftAudioPath } from '#lib/api/drafts.js';
	import { formatClock, formatDb, formatPercent } from '#lib/format.js';
	import { clone, MIN_TRIM_SECONDS, type CloneStep } from '#lib/stores/clone.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';

	const steps: { id: CloneStep; label: string }[] = [
		{ id: 'sample', label: 'Add sample' },
		{ id: 'review', label: 'Check and trim' },
		{ id: 'save', label: 'Name and save' }
	];

	const asrInstalled = $derived(studio.asrEngines.some((engine) => engine.installed));
	const stepIndex = $derived(steps.findIndex((step) => step.id === clone.step));

	let audioEl = $state<HTMLAudioElement | null>(null);
	let audioUrl = $state<string | null>(null);
	let audioLoading = $state(false);
	let audioError = $state('');
	let playingSelection = $state(false);

	async function ensureAudio(): Promise<HTMLAudioElement | null> {
		if (!clone.draft) return null;
		if (audioEl) return audioEl;
		audioLoading = true;
		audioError = '';
		try {
			const blob = await requestBlob(draftAudioPath(clone.draft.draftId));
			audioUrl = URL.createObjectURL(blob);
			const element = new Audio(audioUrl);
			element.addEventListener('ended', () => {
				playingSelection = false;
			});
			element.addEventListener('timeupdate', () => {
				if (playingSelection && element.currentTime >= clone.trimEnd) {
					element.pause();
					playingSelection = false;
				}
			});
			audioEl = element;
			return element;
		} catch (error) {
			audioError = error instanceof Error ? error.message : 'The draft audio could not be loaded.';
			return null;
		} finally {
			audioLoading = false;
		}
	}

	async function playSelection(): Promise<void> {
		const element = await ensureAudio();
		if (!element) return;
		if (playingSelection) {
			element.pause();
			playingSelection = false;
			return;
		}
		element.currentTime = clone.trimStart;
		playingSelection = true;
		try {
			await element.play();
		} catch {
			playingSelection = false;
			audioError = 'This browser could not play the draft audio.';
		}
	}

	function resetAudio(): void {
		audioEl?.pause();
		audioEl = null;
		playingSelection = false;
		if (audioUrl) URL.revokeObjectURL(audioUrl);
		audioUrl = null;
	}

	function handleFile(file: File): void {
		void clone.acceptFile(file);
	}

	function handleRecorded(blob: Blob): void {
		void clone.acceptRecording(blob);
	}

	function handleTrim(start: number, end: number): void {
		clone.setTrim(start, end);
	}

	function close(): void {
		resetAudio();
		void clone.close();
	}

	function finish(): void {
		resetAudio();
		void clone.close();
	}

	$effect(() => {
		if (!clone.open) resetAudio();
	});

	onDestroy(resetAudio);

	async function save(): Promise<void> {
		const ok = await clone.save();
		if (ok) resetAudio();
	}
</script>

<Dialog
	bind:open={clone.open}
	title={clone.mode === 'add-sample' ? 'Add a reference sample' : 'Clone a voice'}
	description={clone.mode === 'add-sample' && clone.target
		? `A second reference clip for “${clone.target.name}”.`
		: 'Three steps: a clean clip, a checked trim, then a name.'}
	size="lg"
	fullMobile
	onclose={close}
>
	<!-- Step indicator -->
	<ol class="mb-4 flex items-center gap-2" aria-label="Clone steps">
		{#each steps as step, index (step.id)}
			<li
				class="flex min-w-0 items-center gap-2"
				aria-current={clone.step === step.id ? 'step' : undefined}
			>
				<span
					class={`metric flex size-5 shrink-0 items-center justify-center rounded-[4px] border text-[10px] ${
						index <= stepIndex
							? 'border-signal bg-signal-soft text-signal'
							: 'border-rule text-ink-muted'
					}`}>{index + 1}</span
				>
				<span
					class={`truncate text-[12px] ${index === stepIndex ? 'font-medium text-ink' : 'text-ink-secondary'}`}
				>
					{step.label}
				</span>
				{#if index < steps.length - 1}
					<span class="hidden h-px w-6 bg-rule sm:block" aria-hidden="true"></span>
				{/if}
			</li>
		{/each}
	</ol>

	{#if clone.step === 'sample'}
		<div class="space-y-4">
			{#if clone.mode === 'add-sample'}
				<p
					class="rounded-control border border-rule bg-surface px-3 py-2 text-[11px] leading-4 text-ink-secondary"
				>
					The engine consumes one reference per voice. This sample is stored with the voice for
					provenance; tick “make primary” below if it should replace the current reference.
				</p>
			{/if}

			{#if !clone.draft}
				<Dropzone
					id="clone-file"
					label={clone.uploading ? 'Uploading…' : 'Drop an audio file or choose one'}
					hint="WAV, FLAC, Ogg/Opus, MP3 or M4A. Six to twenty seconds of clean speech works best. The file is sent to your daemon only."
					accept="audio/*,.wav,.mp3,.flac,.ogg,.oga,.opus,.m4a,.aac,.webm,.mp4"
					disabled={clone.uploading}
					onfile={handleFile}
				/>

				{#if clone.uploading}
					<div class="space-y-1">
						<div class="progress-track">
							<div
								class="progress-fill"
								style={`width: ${Math.round(clone.uploadRatio * 100)}%`}
							></div>
						</div>
						<p class="metric text-[11px] text-ink-secondary" aria-live="polite">
							Uploading {Math.round(clone.uploadRatio * 100)}%
						</p>
					</div>
				{/if}

				<div class="border-t border-rule pt-3">
					<h3 class="mb-2 text-[12px] font-medium text-ink-secondary">Or record a clip</h3>
					<AudioRecorder disabled={clone.uploading} onrecorded={handleRecorded} />
				</div>
			{:else}
				<div class="rounded-control border border-rule bg-surface p-3">
					<div class="flex items-center justify-between gap-3">
						<div class="min-w-0">
							<p class="truncate text-[12px] font-medium text-ink">{clone.fileName}</p>
							<p class="metric text-[11px] text-ink-secondary">
								{formatClock(clone.duration, 2)} · {clone.draft.sampleRate} Hz · {clone.draft
									.channels}
								ch
							</p>
						</div>
						<button type="button" class="btn btn-sm" onclick={() => void playSelection()}>
							{#if playingSelection}
								<Square class="size-3" aria-hidden="true" />
								Stop
							{:else}
								<Play class="size-3" aria-hidden="true" />
								Preview
							{/if}
						</button>
					</div>
				</div>
				<button type="button" class="btn btn-sm btn-ghost" onclick={() => clone.reset()}>
					Replace the sample
				</button>
			{/if}

			{#if clone.error}
				<ErrorNotice error={clone.error} />
				{#if clone.error.code === 'QUOTA_EXCEEDED'}
					<p class="text-[11px] leading-4 text-ink-secondary">
						The daemon allows a fixed number of simultaneous drafts. Close another cloning tab, or
						wait for the draft TTL to expire, then retry.
					</p>
				{/if}
			{/if}
			{#if audioError}
				<p class="text-[11px] text-danger" role="alert">{audioError}</p>
			{/if}
		</div>
	{:else if clone.step === 'review' && clone.draft}
		<div class="space-y-4">
			<TrimWaveform
				peaks={clone.draft.peaks}
				duration={clone.duration}
				start={clone.trimStart}
				end={clone.trimEnd}
				minSeconds={MIN_TRIM_SECONDS}
				onchange={handleTrim}
			/>

			<div class="flex flex-wrap items-center gap-2">
				<button type="button" class="btn btn-sm" onclick={() => void playSelection()}>
					{#if playingSelection}
						<Square class="size-3" aria-hidden="true" />
						Stop
					{:else}
						<Play class="size-3" aria-hidden="true" />
						Play selection
					{/if}
				</button>
				{#if audioLoading}
					<span class="text-[11px] text-ink-muted">Loading draft audio…</span>
				{/if}
				<span class="metric text-[11px] text-ink-secondary">
					peak {formatDb(clone.draft.quality.peakDbfs)} · RMS
					{formatDb(clone.draft.quality.rmsDbfs)} · silence
					{formatPercent(clone.draft.quality.silenceRatio)}
					{#if clone.draft.quality.clipping.detected}
						· {clone.draft.quality.clipping.clippedSamples} clipped samples
					{/if}
				</span>
			</div>

			{#if clone.trimLength < 3}
				<p class="text-[11px] leading-4 text-warning">
					Under three seconds rarely clones well. Extend the selection if the clip allows it.
				</p>
			{/if}

			<div>
				<h3 class="mb-1.5 text-[12px] font-medium text-ink-secondary">Quality report</h3>
				{#if clone.draft.quality.warnings.length === 0}
					<p class="text-[12px] text-ink-secondary">
						The daemon reported no quality warnings for this clip.
					</p>
				{:else}
					<ul class="space-y-1.5">
						{#each clone.draft.quality.warnings as warning (warning.code)}
							<li class="flex items-start gap-2">
								<span
									class={`chip mt-0.5 ${
										warning.severity === 'error'
											? 'chip-danger'
											: warning.severity === 'warning'
												? 'chip-signal'
												: ''
									}`}
								>
									{warning.code.replaceAll('_', ' ')}
								</span>
								<div class="min-w-0">
									<p class="text-[12px] leading-5 text-ink">{warning.message}</p>
									{#if warning.suggestion}
										<p class="text-[11px] leading-4 text-ink-secondary">{warning.suggestion}</p>
									{/if}
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			</div>

			<div class="space-y-1">
				<label class="field-label" for="clone-transcript">Reference transcript</label>
				<textarea
					id="clone-transcript"
					class="textarea min-h-20 text-[13px]"
					bind:value={clone.transcript}
					placeholder="Type exactly what is spoken in the clip."></textarea>
				<p class="text-[11px] leading-4 text-ink-muted">
					{#if clone.draft.suggestedTranscriptEngine}
						Transcribed with <span class="metric">{clone.draft.suggestedTranscriptEngine}</span>.
						Correct anything the decoder got wrong; an accurate transcript improves the clone.
					{:else}
						No ASR engine produced this transcript, so type it yourself or leave it blank.
					{/if}
				</p>
				{#if clone.draft.suggestedTranscriptError}
					<p class="text-[11px] leading-4 text-warning">
						Auto-transcription failed: {clone.draft.suggestedTranscriptError.message}
					</p>
				{/if}
				{#if !asrInstalled}
					<button
						type="button"
						class="btn btn-sm btn-ghost -ml-2"
						onclick={() => {
							close();
							void goto(resolve('models'));
						}}
					>
						Install an ASR engine on Models
					</button>
				{/if}
			</div>

			<label class="flex items-start gap-2 text-[12px] text-ink-secondary">
				<input class="checkbox mt-0.5" type="checkbox" bind:checked={clone.xVectorOnly} />
				<span>
					X-vector only <span class="text-ink-muted">(Advanced)</span> — ignore the transcript and clone
					from timbre alone. Lower fidelity; useful when no transcript exists.
				</span>
			</label>

			{#if audioError}
				<p class="text-[11px] text-danger" role="alert">{audioError}</p>
			{/if}
		</div>
	{:else if clone.step === 'save'}
		<div class="space-y-4">
			{#if clone.savedVoiceId}
				<div class="rounded-control border border-rule bg-surface p-4">
					<p class="text-[14px] font-medium text-ink">
						{clone.mode === 'add-sample' ? 'Sample added' : 'Voice saved'}
					</p>
					<p class="mt-1 text-[12px] text-ink-secondary">
						The voice is in your library and ready to speak.
					</p>
					<div class="mt-3 flex flex-wrap gap-2">
						<button
							type="button"
							class="btn btn-primary"
							disabled={clone.previewing}
							onclick={() => void clone.tryIt()}
						>
							{clone.previewing ? 'Rendering…' : 'Try it'}
						</button>
						<button type="button" class="btn" onclick={finish}>Done</button>
					</div>
					{#if clone.saveError}
						<div class="mt-3">
							<ErrorNotice error={clone.saveError} compact />
						</div>
					{/if}
				</div>
			{:else}
				<div class="space-y-1">
					<label class="field-label" for="clone-name">Voice name</label>
					<input
						id="clone-name"
						class="input"
						type="text"
						maxlength="64"
						bind:value={clone.name}
						placeholder="Narrator"
						aria-invalid={clone.name.trim().length === 0 ? 'true' : undefined}
					/>
					<p class="text-[11px] text-ink-muted">
						Letters, digits, spaces, dots and hyphens; up to 64 characters.
					</p>
				</div>

				<div class="grid gap-3 sm:grid-cols-2">
					<div class="space-y-1">
						<label class="field-label" for="clone-language">Language</label>
						<select id="clone-language" class="select" bind:value={clone.language}>
							{#each clone.languages as language (language)}
								<option value={language}>{language}</option>
							{/each}
						</select>
					</div>
					{#if clone.mode === 'add-sample'}
						<label class="flex items-start gap-2 self-end pb-1 text-[12px] text-ink-secondary">
							<input class="checkbox mt-0.5" type="checkbox" bind:checked={clone.makePrimary} />
							<span>Make this the primary reference</span>
						</label>
					{/if}
				</div>

				<div class="space-y-1">
					<label class="field-label" for="clone-notes">Notes (optional)</label>
					<textarea
						id="clone-notes"
						class="textarea min-h-16 text-[13px]"
						bind:value={clone.notes}
						placeholder="Where the recording came from, its tone, anything to remember."></textarea>
				</div>

				<label
					class="flex items-start gap-2 rounded-control border border-rule bg-surface px-3 py-2.5 text-[12px] leading-5 text-ink"
				>
					<input class="checkbox mt-0.5" type="checkbox" bind:checked={clone.consent} />
					<span>
						I have the right to clone this voice.
						<span class="block text-[11px] leading-4 text-ink-secondary">
							The daemon records the consent timestamp with the voice. Do not clone a voice you do
							not have permission to use.
						</span>
					</span>
				</label>

				{#if clone.trimStart > 0 || clone.trimEnd < clone.duration}
					<p class="metric text-[11px] text-ink-muted">
						Saving {formatClock(clone.trimStart)}–{formatClock(clone.trimEnd)}
						({formatClock(clone.trimLength)})
					</p>
				{/if}

				{#if clone.saveError}
					<ErrorNotice error={clone.saveError} />
				{/if}
			{/if}
		</div>
	{/if}

	{#snippet footer()}
		{#if clone.step === 'sample'}
			<button type="button" class="btn" onclick={close}>Cancel</button>
			<button
				type="button"
				class="btn btn-primary"
				disabled={!clone.canContinue}
				onclick={() => clone.goTo('review')}
			>
				Check and trim
			</button>
		{:else if clone.step === 'review'}
			<button type="button" class="btn" onclick={() => clone.goTo('sample')}>Back</button>
			<button
				type="button"
				class="btn btn-primary"
				disabled={!clone.canContinue}
				onclick={() => clone.goTo('save')}
			>
				Continue
			</button>
		{:else}
			{#if clone.savedVoiceId}
				<button type="button" class="btn btn-primary" onclick={finish}>Done</button>
			{:else}
				<button type="button" class="btn" onclick={() => clone.goTo('review')}>Back</button>
				<button
					type="button"
					class="btn btn-primary"
					disabled={!clone.canSave}
					onclick={() => void save()}
				>
					{clone.saving ? 'Saving…' : clone.mode === 'add-sample' ? 'Add sample' : 'Save voice'}
				</button>
			{/if}
		{/if}
	{/snippet}
</Dialog>
