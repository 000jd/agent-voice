<script lang="ts">
	import History from '@lucide/svelte/icons/history';
	import AudioLines from '@lucide/svelte/icons/audio-lines';
	import { resolve } from '$app/paths';
	import EmptyState from '#lib/components/empty-state.svelte';
	import PageHeader from '#lib/components/page-header.svelte';
	import ClipRow from '#lib/components/studio/clip-row.svelte';
	import { Button } from '#lib/components/ui/button/index.js';
	import { studio } from '#lib/stores/studio.svelte.js';

	let confirming = $state(false);
</script>

<PageHeader
	title="History"
	description="Renders from this browser session. Durable job history is not exposed by the REST API."
>
	{#if studio.sessionClips.length > 0}
		{#if confirming}
			<Button
				variant="destructive"
				size="sm"
				onclick={() => {
					studio.clearSession();
					confirming = false;
				}}
			>
				Clear session
			</Button>
			<Button variant="ghost" size="sm" onclick={() => (confirming = false)}>Cancel</Button>
		{:else}
			<Button variant="outline" size="sm" onclick={() => (confirming = true)}>Clear session</Button>
		{/if}
	{/if}
</PageHeader>

{#if studio.sessionClips.length === 0}
	<EmptyState
		icon={History}
		title="No session renders"
		description="Generate speech or audition a design voice. This page keeps the clips from the current tab only."
	>
		<Button href={resolve('speak')} variant="outline" size="sm">
			<AudioLines class="size-3.5" aria-hidden="true" />
			Open Speak
		</Button>
	</EmptyState>
{:else}
	<div class="rounded-xl border border-border bg-elevated px-3">
		<div class="divide-y divide-border">
			{#each studio.sessionClips as clip (clip.id)}
				<ClipRow {clip} />
			{/each}
		</div>
	</div>
	<p class="mt-2 px-1 text-[10px] text-text-muted">
		Cached and realtime figures come from the daemon's synthesis result. Nothing here is
		reconstructed.
	</p>
{/if}
