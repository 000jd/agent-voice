<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import Cpu from '@lucide/svelte/icons/cpu';
	import Info from '@lucide/svelte/icons/info';
	import Search from '@lucide/svelte/icons/search';
	import ThemeToggle from '#lib/components/common/theme-toggle.svelte';
	import { formatGpuLabel } from '#lib/format.js';
	import { navLabelFor } from '#lib/nav.js';
	import { palette } from '#lib/stores/palette.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';

	let { onhelp }: { onhelp: () => void } = $props();

	const title = $derived(navLabelFor(page.url.pathname));
	const gpuLabel = $derived(studio.gpu ? formatGpuLabel(studio.gpu) : 'GPU —');
	const connectionLabel = $derived(
		studio.connection === 'connected'
			? 'Connected'
			: studio.connection === 'offline'
				? 'Offline'
				: 'Checking'
	);
	const shortcut = $derived(
		typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘K' : 'Ctrl K'
	);
</script>

<header
	class="flex h-14 shrink-0 items-center gap-2 border-b border-rule bg-panel px-3 md:px-4"
	data-testid="top-bar"
>
	<h1 class="min-w-0 truncate text-[15px] font-semibold tracking-[-0.01em] text-ink">{title}</h1>

	<div class="ml-auto flex items-center gap-1.5 md:gap-2">
		<button
			type="button"
			class="btn btn-quiet h-8 gap-2 px-2 text-ink-secondary md:px-2.5"
			aria-label={`Search and commands (${shortcut})`}
			aria-keyshortcuts="Meta+K Control+K"
			onclick={() => palette.show()}
		>
			<Search class="size-3.5" aria-hidden="true" />
			<span class="hidden text-[12px] md:inline">Search</span>
			<kbd class="kbd hidden md:inline-flex">{shortcut}</kbd>
		</button>

		<span
			class="hidden h-8 items-center gap-1.5 rounded-control border border-rule bg-surface px-2 text-[11px] text-ink-secondary sm:flex"
			role="status"
			title={studio.connection === 'offline' ? 'The daemon is not reachable.' : 'Daemon link'}
		>
			<span
				class={`size-1.5 rounded-full ${
					studio.connection === 'connected'
						? 'bg-success'
						: studio.connection === 'offline'
							? 'bg-danger'
							: 'bg-ink-muted'
				}`}
				aria-hidden="true"
			></span>
			{connectionLabel}
		</span>

		<a
			href={resolve('models')}
			class="hidden h-8 items-center gap-1.5 rounded-control border border-rule bg-surface px-2 text-[11px] text-ink-secondary hover:border-rule-strong hover:text-ink lg:flex"
			title={`Queue ${studio.gpu?.queueDepth ?? 0} · in flight ${studio.gpu?.inFlight ?? 0}`}
		>
			<Cpu class="size-3.5" aria-hidden="true" />
			<span class="metric">{gpuLabel}</span>
		</a>

		<ThemeToggle />

		<button
			type="button"
			class="btn btn-ghost btn-icon"
			aria-label="Keyboard shortcuts and privacy"
			title="Keyboard shortcuts and privacy"
			onclick={onhelp}
		>
			<Info class="size-4" aria-hidden="true" />
		</button>
	</div>
</header>
