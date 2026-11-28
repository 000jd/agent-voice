<script lang="ts">
	import Moon from '@lucide/svelte/icons/moon';
	import Sun from '@lucide/svelte/icons/sun';
	import { theme } from '#lib/theme.svelte.js';
</script>

<button
	type="button"
	class="btn btn-ghost btn-icon"
	aria-label={theme.mode === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
	title={theme.mode === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
	onclick={() => theme.toggle()}
>
	{#if theme.mode === 'dark'}
		<Sun class="size-4" aria-hidden="true" />
	{:else}
		<Moon class="size-4" aria-hidden="true" />
	{/if}
</button>
