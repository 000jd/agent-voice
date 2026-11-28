<script lang="ts">
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import RotateCcw from '@lucide/svelte/icons/rotate-ccw';
	import Star from '@lucide/svelte/icons/star';
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import type { HistoryItem } from '#lib/api/types.js';
	import { formatClock, relativeTime, shortText } from '#lib/format.js';
	import { library } from '#lib/stores/library.svelte.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';

	const KIND_LABELS: Record<HistoryItem['kind'], string> = {
		speak: 'Speech',
		script: 'Script',
		design: 'Design',
		transcribe: 'Transcription'
	};

	let { item }: { item: HistoryItem } = $props();

	const player = getPlayer();
	const playing = $derived(player.clip?.id === (item.artifactId ?? item.id) && player.playing);
	const title = $derived(shortText(item.text, 96) || `${KIND_LABELS[item.kind]} take`);
	const meta = $derived(
		[
			KIND_LABELS[item.kind],
			item.voiceName || item.speaker || item.voiceId || '',
			item.engineId,
			formatClock(item.durationSec),
			relativeTime(item.createdAt)
		]
			.filter((part) => part !== '')
			.join(' · ')
	);
</script>

<li class="group flex items-center gap-2 px-2 py-2 md:px-3">
	<button
		type="button"
		class="btn btn-ghost btn-icon shrink-0"
		aria-label={playing ? 'Pause' : `Play ${title}`}
		disabled={item.status === 'failed'}
		onclick={() => {
			if (playing) player.toggle();
			else library.playHistory(item);
		}}
	>
		{#if playing}
			<Pause class="size-3.5" aria-hidden="true" />
		{:else}
			<Play class="size-3.5" aria-hidden="true" />
		{/if}
	</button>
	<div class="min-w-0 flex-1">
		<p class="truncate text-[13px] text-ink">{title}</p>
		<p class="metric truncate text-[11px] text-ink-muted">{meta}</p>
		{#if item.error}
			<p class="truncate text-[11px] text-danger">{item.error.message}</p>
		{/if}
	</div>
	<span class="chip hidden shrink-0 sm:inline-flex">{KIND_LABELS[item.kind]}</span>
	<div class="flex shrink-0 items-center gap-0.5">
		<button
			type="button"
			class="btn btn-ghost btn-icon"
			aria-label={item.favorite ? 'Remove favourite' : 'Favourite'}
			aria-pressed={item.favorite}
			onclick={() => void library.toggleFavorite(item)}
		>
			<Star class={`size-3.5 ${item.favorite ? 'text-signal' : ''}`} aria-hidden="true" />
		</button>
		<button
			type="button"
			class="btn btn-ghost btn-icon"
			aria-label="Rerender with a new take"
			title={item.rerenderable ? 'Rerender with a new take' : 'This entry cannot be rerendered'}
			disabled={!item.rerenderable}
			onclick={() => void library.rerender(item)}
		>
			<RotateCcw class="size-3.5" aria-hidden="true" />
		</button>
		<button
			type="button"
			class="btn btn-ghost btn-icon text-ink-muted hover:text-danger"
			aria-label="Delete from library"
			onclick={() => void library.removeHistory(item)}
		>
			<Trash2 class="size-3.5" aria-hidden="true" />
		</button>
	</div>
</li>
