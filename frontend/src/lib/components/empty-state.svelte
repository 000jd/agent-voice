<script lang="ts">
	import type { Component, Snippet } from 'svelte';

	let {
		icon: Icon,
		title,
		description,
		children
	}: {
		icon: Component;
		title: string;
		description: string;
		children?: Snippet;
	} = $props();
</script>

<div
	class="flex flex-col items-center justify-center rounded-xl border border-border bg-elevated/40 px-6 py-12 text-center"
>
	<span class="mb-3 flex size-10 items-center justify-center rounded-lg bg-panel text-text-muted">
		<Icon class="size-5" aria-hidden="true" />
	</span>
	<h2 class="text-[14px] font-medium">{title}</h2>
	<p class="mt-1 max-w-md text-[12px] leading-5 text-text-secondary">{description}</p>
	{#if children}
		<div class="mt-4">{@render children()}</div>
	{/if}
</div>
