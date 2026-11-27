<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		title,
		description,
		children
	}: {
		title: string;
		description?: string;
		children?: Snippet;
	} = $props();
</script>

<div class="mb-5 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
	<div class="min-w-0">
		<h1 class="text-[22px] leading-7 font-semibold tracking-[-0.015em]">{title}</h1>
		{#if description}
			<p class="mt-1 max-w-3xl text-[13px] leading-5 text-text-secondary">{description}</p>
		{/if}
	</div>
	{#if children}
		<div class="flex shrink-0 items-center gap-2">
			{@render children()}
		</div>
	{/if}
</div>
