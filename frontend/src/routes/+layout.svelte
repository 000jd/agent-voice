<script lang="ts">
	import { onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import '../app.css';
	import favicon from '#lib/assets/favicon.svg';
	import AppShell from '#lib/components/app-shell.svelte';
	import { NAV_ITEMS } from '#lib/nav.js';
	import { theme } from '#lib/theme.svelte.js';
	import type { LayoutProps } from './$types';

	let { children }: LayoutProps = $props();

	const nav = $derived(
		NAV_ITEMS.map((item) => ({
			...item,
			href: resolve(item.href).replace(/\/+$/, '') || '/'
		}))
	);
	const currentPath = $derived(page.url.pathname.replace(/\/+$/, '') || '/');

	onMount(() => {
		theme.init();
	});
</script>

<svelte:head>
	<title>agent-voice</title>
	<meta
		name="description"
		content="Local web UI for the agent-voice headless speech server: synthesis, voices, transcription, models."
	/>
	<link rel="icon" href={favicon} />
</svelte:head>

<AppShell {nav} {currentPath} {children} />
