<script lang="ts">
	import Download from '@lucide/svelte/icons/download';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import Star from '@lucide/svelte/icons/star';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import { describeApiError, requestBlob } from '#lib/api/client.js';
	import type { HistoryItem } from '#lib/api/types.js';
	import {
		downloadBlob,
		formatClock,
		relativeTime,
		sanitizeFilename,
		shortText
	} from '#lib/format.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';
	import { speak } from '#lib/stores/speak.svelte.js';
	import { toasts } from '#lib/stores/toasts.svelte.js';

	const player = getPlayer();

	const compareItems = $derived(
		[speak.compareLeft, speak.compareRight]
			.map((id) => speak.takes.find((item) => item.id === id) ?? null)
			.filter((item): item is HistoryItem => item !== null)
	);

	function isPlaying(item: HistoryItem): boolean {
		return player.clip?.id === (item.artifactId ?? item.id) && player.playing;
	}

	async function download(item: HistoryItem): Promise<void> {
		if (!item.audioUrl) {
			toasts.info('No audio stored', 'This history entry has no artifact.');
			return;
		}
		try {
			const blob = await requestBlob(item.audioUrl);
			downloadBlob(
				`${sanitizeFilename(shortText(item.text, 32) || 'take')}.${item.format || 'wav'}`,
				blob
			);
		} catch (error) {
			toasts.error('Download failed', describeApiError(error).message);
		}
	}
</script>

<section class="flex min-h-0 flex-col border-t border-rule" aria-labelledby="takes-heading">
	<div class="flex shrink-0 items-center gap-2 px-3 py-1.5 md:px-4">
		<h2 id="takes-heading" class="text-[12px] font-medium text-ink-secondary">
			Takes <span class="metric text-[11px] text-ink-muted">{speak.takes.length}</span>
		</h2>
		{#if speak.compareLeft && speak.compareRight}
			<div class="ml-2 flex min-w-0 flex-wrap items-center gap-1.5">
				<span class="chip chip-signal">A/B</span>
				{#each compareItems as item, index (item.id)}
					<button
						type="button"
						class="btn btn-sm"
						onclick={() => speak.playTake(item)}
						title={item.text}
					>
						Play {index === 0 ? 'A' : 'B'}
					</button>
				{/each}
				<button
					type="button"
					class="btn btn-sm btn-ghost"
					onclick={() => {
						speak.setCompare('left', null);
						speak.setCompare('right', null);
					}}
				>
					Clear
				</button>
			</div>
		{/if}
		<button
			type="button"
			class="btn btn-ghost btn-icon ml-auto"
			aria-label="Refresh takes"
			title="Refresh takes"
			onclick={() => void speak.loadTakes()}
		>
			<RefreshCw class="size-3.5" aria-hidden="true" />
		</button>
	</div>

	<div class="max-h-[196px] min-h-0 flex-1 overflow-y-auto px-1 pb-1">
		{#if speak.takesLoading && speak.takes.length === 0}
			<div class="space-y-1 px-2">
				{#each Array.from({ length: 3 }, (_, index) => index) as index (index)}
					<div class="skeleton h-8 w-full"></div>
				{/each}
			</div>
		{:else if speak.takesError}
			<p class="px-3 py-2 text-[12px] text-danger" role="alert">{speak.takesError}</p>
		{:else if speak.takes.length === 0}
			<p class="px-3 py-3 text-[12px] leading-5 text-ink-secondary">
				No takes yet. Generate a line and it is stored here (and in the Library) for the rest of the
				daemon's retention window.
			</p>
		{:else}
			<ul class="divide-y divide-rule">
				{#each speak.takes as take (take.id)}
					<li class="group flex items-center gap-1.5 px-2 py-1">
						<button
							type="button"
							class="btn btn-ghost btn-icon shrink-0"
							aria-label={isPlaying(take) ? 'Pause take' : `Play take ${shortText(take.text, 32)}`}
							onclick={() => {
								if (isPlaying(take)) player.toggle();
								else speak.playTake(take);
							}}
						>
							{#if isPlaying(take)}
								<Pause class="size-3.5" aria-hidden="true" />
							{:else}
								<Play class="size-3.5" aria-hidden="true" />
							{/if}
						</button>
						<div class="min-w-0 flex-1">
							<p class="truncate text-[12px] text-ink">{shortText(take.text, 88) || 'Untitled'}</p>
							<p class="metric truncate text-[11px] text-ink-muted">
								{formatClock(take.durationSec)} · {relativeTime(take.createdAt)}
								{#if take.take !== null}· take {take.take}{/if}
								{#if take.voiceName}· {take.voiceName}{/if}
							</p>
						</div>
						<div class="flex shrink-0 items-center gap-0.5">
							<button
								type="button"
								class={`btn btn-ghost btn-icon ${speak.compareLeft === take.id ? 'text-signal' : ''}`}
								aria-label="Set A for comparison"
								aria-pressed={speak.compareLeft === take.id}
								title="Set A"
								onclick={() => speak.setCompare('left', take.id)}
							>
								<span class="metric text-[11px]">A</span>
							</button>
							<button
								type="button"
								class={`btn btn-ghost btn-icon ${speak.compareRight === take.id ? 'text-signal' : ''}`}
								aria-label="Set B for comparison"
								aria-pressed={speak.compareRight === take.id}
								title="Set B"
								onclick={() => speak.setCompare('right', take.id)}
							>
								<span class="metric text-[11px]">B</span>
							</button>
							<button
								type="button"
								class="btn btn-ghost btn-icon"
								aria-label={take.favorite ? 'Remove favourite' : 'Favourite take'}
								aria-pressed={take.favorite}
								title="Favourite"
								onclick={() => void speak.toggleFavorite(take)}
							>
								<Star class={`size-3.5 ${take.favorite ? 'text-signal' : ''}`} aria-hidden="true" />
							</button>
							<button
								type="button"
								class="btn btn-ghost btn-icon"
								aria-label="Rerender another take"
								title="Rerender with a new take"
								disabled={!take.rerenderable}
								onclick={() => void speak.rerenderTake(take)}
							>
								<RefreshCw class="size-3.5" aria-hidden="true" />
							</button>
							<button
								type="button"
								class="btn btn-ghost btn-icon"
								aria-label="Download take"
								title="Download"
								onclick={() => void download(take)}
							>
								<Download class="size-3.5" aria-hidden="true" />
							</button>
							<button
								type="button"
								class="btn btn-ghost btn-icon text-ink-muted hover:text-danger"
								aria-label="Delete take"
								title="Delete"
								onclick={() => void speak.deleteTake(take)}
							>
								<Trash2 class="size-3.5" aria-hidden="true" />
							</button>
						</div>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</section>
