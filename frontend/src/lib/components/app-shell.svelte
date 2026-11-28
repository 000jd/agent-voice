<script lang="ts">
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import type { Snippet } from 'svelte';
	import Menu from '@lucide/svelte/icons/menu';
	import PanelLeftClose from '@lucide/svelte/icons/panel-left-close';
	import PanelLeftOpen from '@lucide/svelte/icons/panel-left-open';
	import Plus from '@lucide/svelte/icons/plus';
	import Search from '@lucide/svelte/icons/search';
	import BrandMark from '#lib/components/brand-mark.svelte';
	import NavLink from '#lib/components/nav-link.svelte';
	import { Button } from '#lib/components/ui/button/index.js';
	import ConnectionBadge from '#lib/components/studio/connection-badge.svelte';
	import GpuPill from '#lib/components/studio/gpu-pill.svelte';
	import PlayerBar from '#lib/components/studio/player-bar.svelte';
	import ThemeToggle from '#lib/components/theme-toggle.svelte';
	import type { NavItem } from '#lib/nav.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { theme } from '#lib/theme.svelte.js';
	import { cn } from '#lib/utils.js';

	type ShellNavItem = Omit<NavItem, 'href'> & { href: string };
	type ShellGroup = { label: string; items: readonly ShellNavItem[] };

	let {
		groups,
		currentPath,
		children
	}: {
		groups: readonly ShellGroup[];
		currentPath: string;
		children: Snippet;
	} = $props();

	const player = getPlayer();
	const speakHref = resolve('speak').replace(/\/+$/, '');
	const transcribeHref = resolve('transcribe').replace(/\/+$/, '');
	const newSpeechVisible = $derived(currentPath !== speakHref);
	const sections = $derived([
		{
			label: 'Studio',
			href: speakHref,
			active: currentPath === speakHref || currentPath === transcribeHref
		},
		{
			label: 'Explore',
			href: resolve('voices').replace(/\/+$/, ''),
			active: currentPath === resolve('voices').replace(/\/+$/, '')
		},
		{
			label: 'System',
			href: resolve('models').replace(/\/+$/, ''),
			active: currentPath === resolve('models').replace(/\/+$/, '')
		}
	]);
	let search = $state('');

	function onWindowKeyDown(event: KeyboardEvent): void {
		if (event.key === 'Escape') studio.closeMobile();
	}

	function submitSearch(event: SubmitEvent): void {
		event.preventDefault();
		const needle = search.trim();
		void goto(`${resolve('voices')}${needle ? `?q=${encodeURIComponent(needle)}` : ''}`);
	}
</script>

<svelte:window onkeydown={onWindowKeyDown} />

<div class="min-h-dvh bg-canvas text-foreground">
	<header
		class="sticky top-0 z-30 flex h-[52px] items-center gap-3 border-b border-border bg-panel px-3 md:px-5"
	>
		<button
			type="button"
			class="flex size-8 items-center justify-center rounded-lg text-text-secondary hover:bg-elevated-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/30 focus-visible:outline-none md:hidden"
			aria-label={studio.mobileNavOpen ? 'Close navigation' : 'Open navigation'}
			aria-expanded={studio.mobileNavOpen}
			onclick={() => (studio.mobileNavOpen ? studio.closeMobile() : studio.openMobile())}
		>
			<Menu class="size-4" aria-hidden="true" />
		</button>

		<a
			href={resolve('')}
			class="flex shrink-0 items-center gap-2 text-foreground focus-visible:ring-2 focus-visible:ring-ring/30 focus-visible:outline-none"
			aria-label="agent-voice home"
		>
			<BrandMark class="size-5" />
			<span class="hidden text-[13px] font-semibold tracking-[-0.01em] sm:inline">agent-voice</span>
		</a>

		<nav class="hidden items-center gap-1 lg:flex" aria-label="Sections">
			{#each sections as section (section.href)}
				<a
					href={section.href}
					aria-current={section.active ? 'page' : undefined}
					class={cn(
						'rounded-md px-2.5 py-1.5 text-[12px] font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring/30 focus-visible:outline-none',
						section.active
							? 'bg-elevated-hover text-foreground'
							: 'text-text-secondary hover:bg-elevated-hover/60 hover:text-foreground'
					)}
				>
					{section.label}
				</a>
			{/each}
		</nav>

		<form
			class="mx-auto hidden max-w-[360px] flex-1 items-center md:flex"
			role="search"
			onsubmit={submitSearch}
		>
			<div class="relative w-full">
				<Search
					class="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-text-muted"
					aria-hidden="true"
				/>
				<input
					type="search"
					class="h-8 w-full rounded-lg border border-border bg-elevated pr-2.5 pl-8 text-[12px] text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
					placeholder="Search voices"
					aria-label="Search voices"
					bind:value={search}
				/>
			</div>
		</form>

		<div class="ml-auto flex shrink-0 items-center gap-1.5">
			{#if newSpeechVisible}
				<Button href={resolve('speak')} size="sm" class="hidden sm:inline-flex">
					<Plus class="size-3.5" aria-hidden="true" />
					New speech
				</Button>
			{/if}
			<ConnectionBadge state={studio.connection} />
			<ThemeToggle />
		</div>
	</header>

	<div class="flex min-h-[calc(100dvh-52px)]">
		<aside
			class={cn(
				'sticky top-[52px] hidden h-[calc(100dvh-52px)] shrink-0 flex-col border-r border-border bg-panel md:flex',
				studio.railCollapsed ? 'w-16' : 'w-[236px]'
			)}
		>
			<nav class="flex-1 overflow-y-auto px-2.5 py-5" aria-label="Main navigation">
				{#each groups as group (group.label)}
					<div class="mb-5 last:mb-0">
						{#if !studio.railCollapsed}
							<p class="mb-2 px-2.5 text-[11px] font-medium text-text-muted">
								{group.label}
							</p>
						{/if}
						<div class="space-y-0.5">
							{#each group.items as item (item.href)}
								<NavLink
									href={item.href}
									label={item.label}
									icon={item.icon}
									active={currentPath === item.href}
									collapsed={studio.railCollapsed}
								/>
							{/each}
						</div>
					</div>
				{/each}
			</nav>

			<div class="shrink-0 border-t border-border p-2.5">
				<GpuPill
					gpu={studio.gpu}
					error={studio.gpuError}
					compact={studio.railCollapsed}
					unloading={studio.unloading}
					onUnload={() => void studio.unloadAll().catch(() => undefined)}
				/>
				{#if !studio.railCollapsed}
					<p class="mt-3 px-1 text-[10px] leading-4 text-text-muted">
						Local-first studio. No cloud, no account, no telemetry.
					</p>
				{/if}
				<button
					type="button"
					class={cn(
						'mt-2 flex h-7 items-center gap-2 rounded-md px-1.5 text-[11px] text-text-muted hover:bg-elevated-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/30 focus-visible:outline-none',
						studio.railCollapsed ? 'w-9 justify-center px-0' : 'w-full'
					)}
					aria-label={studio.railCollapsed ? 'Expand navigation rail' : 'Collapse navigation rail'}
					title={studio.railCollapsed ? 'Expand navigation rail' : 'Collapse navigation rail'}
					onclick={() => studio.toggleRail()}
				>
					{#if studio.railCollapsed}
						<PanelLeftOpen class="size-3.5" aria-hidden="true" />
					{:else}
						<PanelLeftClose class="size-3.5" aria-hidden="true" />
						<span>Collapse rail</span>
					{/if}
				</button>
			</div>
		</aside>

		<div class="flex min-w-0 flex-1 flex-col">
			{#if studio.mobileNavOpen}
				<button
					type="button"
					class="fixed inset-0 top-[52px] z-20 bg-black/60 md:hidden"
					aria-label="Close navigation"
					onclick={() => studio.closeMobile()}
				></button>
				<div
					class="fixed inset-x-0 top-[52px] z-20 max-h-[calc(100dvh-52px)] overflow-y-auto border-b border-border bg-panel p-4 md:hidden"
				>
					{#each groups as group (group.label)}
						<p class="mb-2 text-[11px] font-medium text-text-muted">{group.label}</p>
						<div class="mb-5 space-y-0.5">
							{#each group.items as item (item.href)}
								<NavLink
									href={item.href}
									label={item.label}
									icon={item.icon}
									active={currentPath === item.href}
									onNavigate={() => studio.closeMobile()}
								/>
							{/each}
						</div>
					{/each}
					<div class="border-t border-border pt-3">
						<GpuPill
							gpu={studio.gpu}
							error={studio.gpuError}
							unloading={studio.unloading}
							onUnload={() => void studio.unloadAll().catch(() => undefined)}
						/>
					</div>
				</div>
			{/if}

			<main
				class={player.active
					? 'min-w-0 flex-1 px-4 py-5 pb-44 md:px-8 md:py-6 md:pb-36'
					: 'min-w-0 flex-1 px-4 py-5 md:px-8 md:py-6'}
			>
				<div class="mx-auto w-full max-w-[1560px]">
					{@render children()}
				</div>
			</main>
		</div>
	</div>
</div>

{#key theme.mode}
	<PlayerBar railCollapsed={studio.railCollapsed} />
{/key}
