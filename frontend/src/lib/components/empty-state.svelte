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
	class="flex flex-col items-center justify-center rounded-xl border border-dashed px-6 py-14 text-center"
>
	<span
		class="mb-4 flex size-12 items-center justify-center rounded-full bg-muted text-muted-foreground"
	>
		<Icon class="size-6" aria-hidden="true" />
	</span>
	<h2 class="text-sm font-semibold">{title}</h2>
	<p class="mt-1 max-w-md text-sm text-muted-foreground">{description}</p>
	{#if children}
		<div class="mt-5">{@render children()}</div>
	{/if}
</div>
