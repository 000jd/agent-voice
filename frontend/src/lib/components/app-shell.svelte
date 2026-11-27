<script lang="ts">
	import AudioLines from '@lucide/svelte/icons/audio-lines';
	import type { Component, Snippet } from 'svelte';
	import NavLink from './nav-link.svelte';
	import ThemeToggle from './theme-toggle.svelte';

	type ShellNavItem = { label: string; href: string; icon: Component };

	let {
		nav,
		currentPath,
		children
	}: {
		nav: readonly ShellNavItem[];
		currentPath: string;
		children: Snippet;
	} = $props();
</script>

<div class="flex min-h-dvh bg-background text-foreground">
	<aside class="hidden w-64 shrink-0 border-r border-sidebar-border bg-sidebar md:flex md:flex-col">
		<div class="flex h-16 items-center gap-3 border-b border-sidebar-border px-5">
			<span
				class="flex size-9 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground"
			>
				<AudioLines class="size-5" aria-hidden="true" />
			</span>
			<div class="min-w-0">
				<p class="truncate text-sm font-semibold">agent-voice</p>
				<p class="truncate text-xs text-muted-foreground">headless speech server</p>
			</div>
		</div>

		<nav class="flex-1 space-y-1 overflow-y-auto p-3" aria-label="Main navigation">
			{#each nav as item (item.href)}
				<NavLink
					href={item.href}
					label={item.label}
					icon={item.icon}
					active={currentPath === item.href}
				/>
			{/each}
		</nav>

		<div class="border-t border-sidebar-border p-4">
			<p class="text-xs font-medium text-sidebar-foreground">Local-first by design</p>
			<p class="mt-1 text-xs leading-relaxed text-muted-foreground">
				Same-origin REST. No cloud, no account, no telemetry.
			</p>
		</div>
	</aside>

	<div class="flex min-w-0 flex-1 flex-col">
		<header class="flex h-16 items-center justify-between gap-3 border-b px-4 md:px-8">
			<div class="flex min-w-0 items-center gap-3 md:hidden">
				<span
					class="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground"
				>
					<AudioLines class="size-4" aria-hidden="true" />
				</span>
				<span class="truncate text-sm font-semibold">agent-voice</span>
			</div>
			<div class="hidden min-w-0 items-center gap-2 md:flex">
				<span class="text-sm font-medium">Web UI</span>
				<span
					class="rounded-full border border-border px-2 py-0.5 text-[10px] font-medium tracking-wide text-muted-foreground uppercase"
				>
					Preview
				</span>
			</div>
			<ThemeToggle />
		</header>

		<nav
			class="flex gap-1 overflow-x-auto border-b px-3 py-2 md:hidden"
			aria-label="Main navigation"
		>
			{#each nav as item (item.href)}
				<NavLink
					href={item.href}
					label={item.label}
					icon={item.icon}
					active={currentPath === item.href}
				/>
			{/each}
		</nav>

		<main class="flex-1 p-4 md:p-8">
			{@render children()}
		</main>

		<footer class="border-t px-4 py-3 text-xs text-muted-foreground md:px-8">
			Same-origin REST client of the local daemon. Audio and API calls stay on this machine unless
			you expose it.
		</footer>
	</div>
</div>
