<script lang="ts">
	import Play from '@lucide/svelte/icons/play';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import AudioRecorder from '#lib/components/common/audio-recorder.svelte';
	import Dropzone from '#lib/components/common/dropzone.svelte';
	import ErrorNotice from '#lib/components/common/error-notice.svelte';
	import { formatClock, relativeTime, shortText } from '#lib/format.js';
	import { transcribe } from '#lib/stores/transcribe.svelte.js';

	let showRecorder = $state(false);

	function onFile(file: File): void {
		void transcribe.transcribe(file);
	}

	function onRecorded(blob: Blob): void {
		const type = blob.type || 'audio/webm';
		const extension = type.includes('ogg') ? 'ogg' : type.includes('mp4') ? 'm4a' : 'webm';
		const file = new File([blob], `recording-${Date.now()}.${extension}`, { type });
		showRecorder = false;
		void transcribe.transcribe(file);
	}
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="shrink-0 space-y-2 border-b border-rule p-3">
		<Dropzone
			id="transcribe-file"
			compact
			label={transcribe.uploading ? 'Transcribing…' : 'Drop audio or video, or choose a file'}
			hint="WAV, FLAC, Ogg, MP3, M4A, MP4, WebM"
			accept="audio/*,video/*,.wav,.mp3,.flac,.ogg,.opus,.m4a,.aac,.mp4,.webm,.mkv,.mov"
			disabled={transcribe.uploading}
			onfile={onFile}
		/>

		{#if transcribe.uploading}
			<div class="space-y-1">
				<div class="progress-track">
					<div
						class="progress-fill"
						style={`width: ${Math.round(transcribe.uploadRatio * 100)}%`}
					></div>
				</div>
				<p class="metric text-[11px] text-ink-secondary" aria-live="polite">
					Uploading {Math.round(transcribe.uploadRatio * 100)}%
				</p>
			</div>
		{/if}

		<div class="flex items-center gap-2">
			<button
				type="button"
				class="btn btn-sm"
				aria-expanded={showRecorder}
				onclick={() => (showRecorder = !showRecorder)}
			>
				{showRecorder ? 'Hide recorder' : 'Record'}
			</button>
			<span class="metric min-w-0 truncate text-[11px] text-ink-muted">
				{transcribe.engineId || 'no ASR engine'} · {transcribe.modelAlias || '—'}
			</span>
		</div>

		{#if showRecorder}
			<div class="rounded-control border border-rule bg-surface p-2">
				<AudioRecorder disabled={transcribe.uploading} onrecorded={onRecorded} />
			</div>
		{/if}

		{#if transcribe.uploadError}
			<ErrorNotice error={transcribe.uploadError} compact />
		{/if}
	</div>

	<div class="min-h-0 flex-1 overflow-y-auto">
		<div class="flex items-center justify-between gap-2 px-3 py-1.5">
			<h2 class="text-[12px] font-medium text-ink-secondary">
				Captures <span class="metric text-[11px] text-ink-muted">{transcribe.captures.length}</span>
			</h2>
			<button
				type="button"
				class="text-[11px] text-ink-secondary hover:text-ink"
				onclick={() => void transcribe.loadCaptures()}
			>
				Refresh
			</button>
		</div>

		{#if transcribe.listLoading && transcribe.captures.length === 0}
			<div class="space-y-1 px-3">
				{#each Array.from({ length: 4 }, (_, index) => index) as index (index)}
					<div class="skeleton h-9 w-full"></div>
				{/each}
			</div>
		{:else if transcribe.captures.length === 0}
			<p class="px-3 py-3 text-[12px] leading-5 text-ink-secondary">
				Nothing transcribed yet. The first upload creates a capture that stays here until the
				retention limit removes it.
			</p>
		{:else}
			<ul class="divide-y divide-rule">
				{#each transcribe.captures as capture (capture.id)}
					<li class="group flex items-center gap-1 pl-1">
						<button
							type="button"
							class="btn btn-ghost btn-icon shrink-0"
							aria-label={`Play ${capture.title || 'capture'}`}
							onclick={() => transcribe.playCapture(capture)}
						>
							<Play class="size-3.5" aria-hidden="true" />
						</button>
						<button
							type="button"
							class={`min-w-0 flex-1 rounded-control px-1.5 py-1.5 text-left ${
								transcribe.selectedId === capture.id
									? 'bg-signal-soft ring-1 ring-signal'
									: 'hover:bg-surface-hover'
							}`}
							aria-pressed={transcribe.selectedId === capture.id}
							onclick={() => void transcribe.select(capture.id)}
						>
							<span class="block truncate text-[12px] text-ink">
								{shortText(capture.title || capture.transcript || 'Untitled capture', 48)}
							</span>
							<span class="metric block truncate text-[11px] text-ink-muted">
								{formatClock(capture.durationSec)} · {capture.segmentsCount} seg · {relativeTime(
									capture.createdAt
								)}
							</span>
						</button>
						<button
							type="button"
							class="btn btn-ghost btn-icon shrink-0 opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
							aria-label={`Delete ${capture.title || 'capture'}`}
							onclick={() => void transcribe.remove(capture.id)}
						>
							<Trash2 class="size-3.5" aria-hidden="true" />
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</div>
