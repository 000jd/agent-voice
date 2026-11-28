<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import '../app.css';
	import favicon from '#lib/assets/favicon.svg';
	import AppShell from '#lib/components/shell/app-shell.svelte';
	import AuthGate from '#lib/components/shell/auth-gate.svelte';
	import { navLabelFor } from '#lib/nav.js';
	import { auth } from '#lib/stores/auth.svelte.js';
	import { events } from '#lib/stores/events.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { theme } from '#lib/theme.svelte.js';
	import type { LayoutProps } from './$types';

	let { children }: LayoutProps = $props();

	onMount(() => {
		theme.init();
		void auth.init();
	});

	$effect(() => {
		if (auth.state !== 'ready') return;
		studio.startPolling();
		events.start();
		return () => {
			studio.stopPolling();
			events.stop();
		};
	});

	const title = $derived(navLabelFor(page.url.pathname));
</script>

<svelte:head>
	<title>{title} · agent-voice</title>
	<meta
		name="description"
		content="Local Studio for the agent-voice speech daemon: speak, clone voices, transcribe and manage models."
	/>
	<link rel="icon" href={favicon} />
</svelte:head>

{#if auth.state === 'ready'}
	<AppShell>{@render children()}</AppShell>
{:else}
	<AuthGate />
{/if}
