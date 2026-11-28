<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		title,
		description = '',
		icon,
		children
	}: {
		title: string;
		description?: string;
		icon?: Snippet;
		children?: Snippet;
	} = $props();
</script>

<div class="flex flex-col items-start gap-3 rounded-panel border border-rule bg-surface p-6">
	{#if icon}
		<div class="text-ink-muted">
			{@render icon()}
		</div>
	{/if}
	<div class="max-w-[52ch]">
		<p class="text-[14px] font-medium text-ink">{title}</p>
		{#if description}
			<p class="mt-1 text-[12px] leading-5 text-ink-secondary">{description}</p>
		{/if}
	</div>
	{#if children}
		<div class="flex flex-wrap items-center gap-2">{@render children()}</div>
	{/if}
</div>
