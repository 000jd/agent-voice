<script lang="ts">
	import Moon from '@lucide/svelte/icons/moon';
	import Sun from '@lucide/svelte/icons/sun';
	import { theme } from '#lib/theme.svelte.js';

	const label = $derived(theme.mode === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
</script>

<button
	type="button"
	class="inline-flex size-9 items-center justify-center rounded-lg border border-border bg-background text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
	aria-label={label}
	title={label}
	onclick={() => theme.toggle()}
>
	{#if theme.mode === 'dark'}
		<Sun class="size-4" aria-hidden="true" />
	{:else}
		<Moon class="size-4" aria-hidden="true" />
	{/if}
</button>
