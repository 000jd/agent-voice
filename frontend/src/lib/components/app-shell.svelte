<script lang="ts">
	import Menu from '@lucide/svelte/icons/menu';
	import PanelLeftClose from '@lucide/svelte/icons/panel-left-close';
	import PanelLeftOpen from '@lucide/svelte/icons/panel-left-open';
	import Plus from '@lucide/svelte/icons/plus';
	import { resolve } from '$app/paths';
	import type { Snippet } from 'svelte';
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
		title,
		currentPath,
		children
	}: {
		groups: readonly ShellGroup[];
		title: string;
		currentPath: string;
		children: Snippet;
	} = $props();

	const player = getPlayer();
	const speakHref = resolve('speak').replace(/\/+$/, '');
	const newSpeechVisible = $derived(currentPath !== speakHref);

	function onWindowKeyDown(event: KeyboardEvent): void {
		if (event.key === 'Escape') studio.closeMobile();
	}
</script>

<svelte:window onkeydown={onWindowKeyDown} />

<div class="flex min-h-dvh bg-canvas text-foreground">
	<aside
		class={cn(
			'sticky top-0 hidden h-dvh shrink-0 flex-col border-r border-border bg-panel md:flex',
			studio.railCollapsed ? 'w-16' : 'w-[220px]'
		)}
	>
		<div
			class={cn(
				'flex h-[52px] shrink-0 items-center border-b border-border',
				studio.railCollapsed ? 'justify-center px-2' : 'gap-2.5 px-3.5'
			)}
		>
			<span
				class="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground"
			>
				<BrandMark class="size-4" />
			</span>
			{#if !studio.railCollapsed}
				<div class="min-w-0">
					<p class="truncate text-[13px] font-semibold tracking-[-0.01em]">agent-voice</p>
					<p class="truncate text-[10px] text-text-muted">local speech studio</p>
				</div>
			{/if}
		</div>

		<nav class="flex-1 overflow-y-auto px-2 py-3" aria-label="Main navigation">
			{#each groups as group (group.label)}
				<div class="mb-4 last:mb-0">
					{#if !studio.railCollapsed}
						<p
							class="mb-1.5 px-2.5 text-[10px] font-medium tracking-[0.1em] text-text-muted uppercase"
						>
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
				<p class="mt-2.5 px-1 text-[10px] leading-4 text-text-muted">
					Loopback REST · no cloud · no telemetry
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
		<header
			class="sticky top-0 z-20 flex h-[52px] shrink-0 items-center justify-between gap-3 border-b border-border bg-canvas/95 px-4 md:px-6"
		>
			<div class="flex min-w-0 items-center gap-2.5">
				<button
					type="button"
					class="flex size-8 items-center justify-center rounded-lg text-text-secondary hover:bg-elevated-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/30 focus-visible:outline-none md:hidden"
					aria-label={studio.mobileNavOpen ? 'Close navigation' : 'Open navigation'}
					aria-expanded={studio.mobileNavOpen}
					onclick={() => (studio.mobileNavOpen ? studio.closeMobile() : studio.openMobile())}
				>
					<Menu class="size-4" aria-hidden="true" />
				</button>
				<span
					class="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground md:hidden"
				>
					<BrandMark class="size-3.5" />
				</span>
				<div class="min-w-0">
					<p
						class="hidden text-[10px] font-medium tracking-[0.1em] text-text-muted uppercase md:block"
					>
						Studio
					</p>
					<h1 class="truncate text-[13px] font-medium text-foreground md:text-[14px]">{title}</h1>
				</div>
			</div>

			<div class="flex shrink-0 items-center gap-1.5">
				{#if newSpeechVisible}
					<Button href={resolve('speak')} variant="outline" size="sm" class="hidden sm:inline-flex">
						<Plus class="size-3.5" aria-hidden="true" />
						New speech
					</Button>
				{/if}
				<ConnectionBadge state={studio.connection} />
				<ThemeToggle />
			</div>
		</header>

		{#if studio.mobileNavOpen}
			<button
				type="button"
				class="fixed inset-0 top-[52px] z-30 bg-black/60 md:hidden"
				aria-label="Close navigation"
				onclick={() => studio.closeMobile()}
			></button>
			<div
				class="fixed inset-x-0 top-[52px] z-40 max-h-[calc(100dvh-52px)] overflow-y-auto border-b border-border bg-panel p-4 md:hidden"
			>
				{#each groups as group (group.label)}
					<p class="mb-2 text-[10px] font-medium tracking-[0.1em] text-text-muted uppercase">
						{group.label}
					</p>
					<div class="mb-4 space-y-0.5">
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
				? 'min-w-0 flex-1 px-4 py-5 pb-40 md:px-8 md:py-7 md:pb-32'
				: 'min-w-0 flex-1 px-4 py-5 md:px-8 md:py-7'}
		>
			<div class="mx-auto w-full max-w-[1600px]">
				{@render children()}
			</div>
		</main>
	</div>
</div>

{#key theme.mode}
	<PlayerBar />
{/key}
