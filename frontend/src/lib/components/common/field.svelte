<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		label,
		forId,
		hint = '',
		error = '',
		children
	}: {
		label: string;
		forId: string;
		hint?: string;
		error?: string;
		children: Snippet;
	} = $props();
</script>

<div class="space-y-1">
	<label class="field-label" for={forId}>{label}</label>
	{@render children()}
	{#if hint}
		<p class="text-[11px] leading-4 text-ink-muted">{hint}</p>
	{/if}
	{#if error}
		<p class="text-[11px] leading-4 text-danger" role="alert">{error}</p>
	{/if}
</div>
