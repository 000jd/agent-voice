<script lang="ts">
	import type { Snippet } from 'svelte';
	import CommandPalette from '#lib/components/shell/command-palette.svelte';
	import HelpDialog from '#lib/components/shell/help-dialog.svelte';
	import PlayerBar from '#lib/components/shell/player-bar.svelte';
	import Rail from '#lib/components/shell/rail.svelte';
	import ToastHost from '#lib/components/shell/toast-host.svelte';
	import TopBar from '#lib/components/shell/top-bar.svelte';
	import { getPlayer } from '#lib/stores/player.svelte.js';
	import { palette } from '#lib/stores/palette.svelte.js';

	let { children }: { children: Snippet } = $props();

	const player = getPlayer();
	let helpOpen = $state(false);
	let railCollapsed = $state(readRailCollapsed());

	function readRailCollapsed(): boolean {
		if (typeof localStorage === 'undefined') return false;
		return localStorage.getItem('agent-voice-rail-collapsed') === '1';
	}

	function persistRail(collapsed: boolean): void {
		try {
			localStorage.setItem('agent-voice-rail-collapsed', collapsed ? '1' : '0');
		} catch {
			// A blocked storage backend only loses the preference for this tab.
		}
	}

	function editable(target: EventTarget | null): boolean {
		if (!(target instanceof HTMLElement)) return false;
		return (
			target.tagName === 'INPUT' ||
			target.tagName === 'TEXTAREA' ||
			target.tagName === 'SELECT' ||
			target.isContentEditable
		);
	}

	function onKeyDown(event: KeyboardEvent): void {
		const modified = event.metaKey || event.ctrlKey;
		if (modified && event.key.toLowerCase() === 'k') {
			event.preventDefault();
			palette.toggle();
			return;
		}
		if (event.key === '?' && !modified && !editable(event.target)) {
			event.preventDefault();
			helpOpen = true;
			return;
		}
		if (event.code === 'Space' && !event.repeat && !modified) {
			if (editable(event.target) || palette.open || helpOpen) return;
			if (!player.active) return;
			event.preventDefault();
			player.toggle();
		}
	}
	$effect(() => {
		persistRail(railCollapsed);
	});
</script>

<svelte:window onkeydown={onKeyDown} />

<a
	href="#main"
	class="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-control focus:border focus:border-rule-strong focus:bg-surface focus:px-3 focus:py-2 focus:text-[12px] focus:text-ink"
>
	Skip to content
</a>

<div class="flex h-dvh flex-col overflow-hidden bg-canvas md:flex-row">
	<Rail bind:collapsed={railCollapsed} />
	<div class="flex min-h-0 min-w-0 flex-1 flex-col">
		<TopBar onhelp={() => (helpOpen = true)} />
		<main id="main" tabindex="-1" class="min-h-0 min-w-0 flex-1 overflow-hidden outline-none">
			{@render children()}
		</main>
		<PlayerBar />
	</div>
</div>

<CommandPalette onhelp={() => (helpOpen = true)} />
<HelpDialog bind:open={helpOpen} />
<ToastHost />
