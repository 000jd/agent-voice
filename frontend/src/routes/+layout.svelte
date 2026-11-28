<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import '../app.css';
	import favicon from '#lib/assets/favicon.svg';
	import AppShell from '#lib/components/app-shell.svelte';
	import { NAV_GROUPS } from '#lib/nav.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { theme } from '#lib/theme.svelte.js';
	import type { LayoutProps } from './$types';

	let { children }: LayoutProps = $props();

	const groups = $derived(
		NAV_GROUPS.map((group) => ({
			...group,
			items: group.items.map((item) => ({
				...item,
				href: resolve(item.href).replace(/\/+$/, '') || '/'
			}))
		}))
	);
	const currentPath = $derived(page.url.pathname.replace(/\/+$/, '') || '/');
	const title = $derived(
		NAV_GROUPS.flatMap((group) => group.items).find(
			(item) => (resolve(item.href).replace(/\/+$/, '') || '/') === currentPath
		)?.label ?? 'Studio'
	);

	onMount(() => {
		theme.init();
		studio.startPolling();
	});

	onDestroy(() => {
		studio.stopPolling();
	});
</script>

<svelte:head>
	<title>{title} · agent-voice</title>
	<meta
		name="description"
		content="Local studio for the agent-voice headless speech server: synthesis, voices, transcription, models."
	/>
	<link rel="icon" href={favicon} />
</svelte:head>

<AppShell {groups} {currentPath} {children} />
