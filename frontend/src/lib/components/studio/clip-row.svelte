<script lang="ts">
	import Download from '@lucide/svelte/icons/download';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import type { ClipSummary } from '#lib/api/types.js';
	import { cacheBadge, formatClock } from '#lib/format.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';

	let { clip, showTitle = true }: { clip: ClipSummary; showTitle?: boolean } = $props();

	const player = getPlayer();
	const active = $derived(player.clip?.id === clip.id);
</script>

<div class="flex items-center gap-2.5 py-2">
	<button
		type="button"
		class="flex size-7 shrink-0 items-center justify-center rounded-full border border-border bg-panel text-text-secondary hover:border-primary/40 hover:bg-primary/10 hover:text-primary focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none"
		aria-label={active && player.playing ? `Pause ${clip.title}` : `Play ${clip.title}`}
		onclick={() => (active ? player.toggle() : void player.play(clip))}
	>
		{#if active && player.playing}
			<Pause class="size-3" aria-hidden="true" />
		{:else}
			<Play class="size-3 translate-x-px" aria-hidden="true" />
		{/if}
	</button>
	<div class="min-w-0 flex-1">
		{#if showTitle}
			<p class="truncate text-[11px] font-medium text-foreground">{clip.title}</p>
		{/if}
		<p class="truncate font-mono text-[10px] text-text-muted">
			{formatClock(clip.durationSec)}
			<span class="px-1">·</span>
			{cacheBadge(clip)}
			{#if clip.speaker}
				<span class="px-1">·</span>
				{clip.speaker}
			{/if}
		</p>
	</div>
	<button
		type="button"
		class="flex size-6 items-center justify-center rounded-md text-text-muted hover:bg-elevated-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none"
		aria-label={`Download ${clip.title}`}
		title="Download audio"
		onclick={() => void player.download(clip)}
	>
		<Download class="size-3.5" aria-hidden="true" />
	</button>
</div>
