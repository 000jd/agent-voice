<script lang="ts">
	import Clock from '@lucide/svelte/icons/clock';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import EmptyState from '#lib/components/common/empty-state.svelte';
	import ErrorNotice from '#lib/components/common/error-notice.svelte';
	import { clipFromCapture } from '#lib/clips.js';
	import { formatClock, formatTimestamp } from '#lib/format.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';
	import { transcribe } from '#lib/stores/transcribe.svelte.js';

	let transcriptDraft = $state('');
	let titleDraft = $state('');
	let lastId: string | null = null;

	const detail = $derived(transcribe.detail);

	$effect(() => {
		const current = transcribe.detail;
		if (!current) {
			if (lastId !== null) {
				lastId = null;
				transcriptDraft = '';
				titleDraft = '';
			}
			return;
		}
		if (current.id === lastId) return;
		lastId = current.id;
		transcriptDraft = current.transcript;
		titleDraft = current.title;
	});

	function seekTo(seconds: number): void {
		const current = transcribe.detail;
		if (!current) return;
		const player = getPlayer();
		const clip = clipFromCapture(current);
		if (player.clip?.id !== clip.id) {
			void player.play(clip).then(() => player.seekSeconds(seconds));
		} else {
			player.seekSeconds(seconds);
		}
	}
</script>

{#snippet clockIcon()}
	<Clock class="size-5" aria-hidden="true" />
{/snippet}

<div class="flex h-full min-h-0 flex-col">
	{#if transcribe.detailLoading && !detail}
		<div class="space-y-3 p-4">
			<div class="skeleton h-4 w-1/3"></div>
			<div class="skeleton h-40 w-full"></div>
			<div class="skeleton h-24 w-full"></div>
		</div>
	{:else if transcribe.detailError}
		<div class="p-4">
			<ErrorNotice error={transcribe.detailError} onRetry={() => void transcribe.loadCaptures()} />
		</div>
	{:else if !detail}
		<div class="p-4">
			<EmptyState
				title="No capture selected"
				description="Upload or record audio on the left, or pick a capture from the list. The transcript, timed segments and server-side subtitle exports appear here."
				icon={clockIcon}
			/>
		</div>
	{:else}
		<div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-rule px-3 py-2 md:px-4">
			<div class="min-w-[180px] flex-1">
				<label class="sr-only" for="capture-title">Capture title</label>
				<input
					id="capture-title"
					class="input h-8 text-[13px]"
					type="text"
					bind:value={titleDraft}
					oninput={() => transcribe.markDirty()}
				/>
			</div>
			<button type="button" class="btn btn-sm" onclick={() => transcribe.playCapture(detail)}>
				Play capture
			</button>
			<button
				type="button"
				class="btn btn-sm btn-primary"
				disabled={transcribe.saving || !transcribe.dirty}
				onclick={() => void transcribe.saveTranscript(transcriptDraft, titleDraft)}
			>
				{transcribe.saving ? 'Saving…' : transcribe.dirty ? 'Save transcript' : 'Saved'}
			</button>
			<button
				type="button"
				class="btn btn-sm btn-ghost btn-icon text-ink-muted hover:text-danger"
				aria-label="Delete capture"
				title="Delete capture"
				onclick={() => void transcribe.remove(detail.id)}
			>
				<Trash2 class="size-3.5" aria-hidden="true" />
			</button>
		</div>

		{#if transcribe.saveError}
			<div class="px-3 pt-2 md:px-4">
				<ErrorNotice error={transcribe.saveError} compact />
			</div>
		{/if}

		<div class="min-h-0 flex-1 overflow-y-auto px-3 py-3 md:px-4">
			<div class="flex flex-wrap items-center gap-2 text-[11px] text-ink-muted">
				<span class="metric">{formatClock(detail.durationSec)}</span>
				<span aria-hidden="true">·</span>
				<span class="metric">{detail.segmentsCount} segments</span>
				<span aria-hidden="true">·</span>
				<span class="metric">{detail.engineId}</span>
				<span aria-hidden="true">·</span>
				<span class="metric">{detail.modelAlias}</span>
				{#if detail.language}
					<span aria-hidden="true">·</span>
					<span>{detail.language}</span>
				{/if}
				<span class="chip ml-auto">{detail.source === 'upload' ? 'upload' : 'local path'}</span>
			</div>

			{#if detail.truncated || detail.segmentsOmitted > 0}
				<p
					class="mt-2 rounded-control border border-rule bg-surface px-3 py-2 text-[11px] leading-4 text-warning"
				>
					The engine flagged a truncated decode{#if detail.segmentsOmitted > 0}; {detail.segmentsOmitted}
						segments are not in this preview{/if}. The full transcript is in the capture's JSON
					export.
				</p>
			{/if}

			<div class="mt-3 space-y-1">
				<label class="field-label" for="transcript-text">Transcript</label>
				<textarea
					id="transcript-text"
					class="textarea min-h-[220px] text-[14px] leading-6"
					bind:value={transcriptDraft}
					oninput={() => transcribe.markDirty()}
					spellcheck="true"></textarea>
				<p class="metric text-[11px] text-ink-muted">
					{transcriptDraft.length} chars · edits are saved to the daemon, not just this tab.
				</p>
			</div>

			<section class="mt-4" aria-labelledby="segments-heading">
				<h2 id="segments-heading" class="mb-1.5 text-[12px] font-medium text-ink-secondary">
					Timed segments
				</h2>
				{#if detail.segments.length === 0}
					<p class="text-[12px] text-ink-secondary">
						This engine returned no timed segments, so subtitle exports will be empty. JSON still
						carries the transcript.
					</p>
				{:else}
					<ol class="divide-y divide-rule rounded-control border border-rule">
						{#each detail.segments as segment (segment.index)}
							<li class="flex items-start gap-2 px-2 py-1.5">
								<button
									type="button"
									class="metric shrink-0 rounded-[3px] px-1 text-[11px] text-ink-secondary hover:bg-surface-hover hover:text-ink"
									aria-label={`Play from ${formatTimestamp(segment.startSec)}`}
									onclick={() => seekTo(segment.startSec)}
								>
									{formatTimestamp(segment.startSec)}
								</button>
								<p class="min-w-0 flex-1 text-[12px] leading-5 text-ink">{segment.text}</p>
							</li>
						{/each}
					</ol>
				{/if}
			</section>
		</div>
	{/if}
</div>
