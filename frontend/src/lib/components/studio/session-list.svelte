<script lang="ts">
	import type { ClipSummary } from '#lib/api/types.js';
	import ClipRow from './clip-row.svelte';

	let {
		clips,
		empty = 'No renders in this session yet.'
	}: { clips: readonly ClipSummary[]; empty?: string } = $props();
</script>

{#if clips.length === 0}
	<p class="px-1 py-6 text-center text-[11px] text-text-muted">{empty}</p>
{:else}
	<div class="divide-y divide-border">
		{#each clips as clip (clip.id)}
			<ClipRow {clip} />
		{/each}
	</div>
{/if}
