<script lang="ts">
	import Moon from '@lucide/svelte/icons/moon';
	import Sun from '@lucide/svelte/icons/sun';
	import { theme } from '#lib/theme.svelte.js';

	const label = $derived(theme.mode === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
</script>

<button
	type="button"
	class="flex size-8 items-center justify-center rounded-lg border border-transparent text-text-secondary transition-colors hover:bg-elevated-hover hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/40 focus-visible:outline-none"
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
