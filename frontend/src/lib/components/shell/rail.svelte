<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import ChevronLeft from '@lucide/svelte/icons/chevron-left';
	import ChevronRight from '@lucide/svelte/icons/chevron-right';
	import { NAV_ITEMS } from '#lib/nav.js';

	let { collapsed = $bindable(false) }: { collapsed?: boolean } = $props();

	const currentPath = $derived(page.url.pathname.replace(/\/+$/, '') || '/');

	function hrefFor(href: (typeof NAV_ITEMS)[number]['href']): string {
		return resolve(href).replace(/\/+$/, '') || '/';
	}

	const items = $derived(
		NAV_ITEMS.map((item) => {
			const href = hrefFor(item.href);
			return { ...item, href, active: currentPath === href };
		})
	);
</script>

<nav
	aria-label="Studio sections"
	class="shrink-0 border-rule bg-panel max-md:order-last max-md:border-t md:h-dvh md:border-r"
>
	<!-- Desktop rail: 224px expanded, 64px collapsed. -->
	<div
		class={`hidden h-full flex-col md:flex ${collapsed ? 'w-16' : 'w-[224px]'}`}
		data-testid="rail"
	>
		<a
			href={hrefFor('speak')}
			class="flex h-14 shrink-0 items-center gap-2.5 border-b border-rule px-4 focus-visible:outline-offset-[-2px]"
			aria-label="agent-voice Studio"
		>
			<span class="h-4 w-1 shrink-0 rounded-[1px] bg-signal" aria-hidden="true"></span>
			{#if !collapsed}
				<span class="truncate text-[13px] font-semibold tracking-[-0.01em] text-ink">
					agent-voice
				</span>
				<span class="metric ml-auto text-[10px] text-ink-muted">studio</span>
			{/if}
		</a>

		<ul class="flex flex-1 flex-col gap-0.5 overflow-y-auto p-2">
			{#each items as item (item.href)}
				<li>
					<a
						href={item.href}
						aria-current={item.active ? 'page' : undefined}
						title={collapsed ? item.label : undefined}
						class={`flex h-9 items-center gap-2.5 rounded-control px-2.5 text-[13px] transition-colors ${
							item.active
								? 'bg-surface font-medium text-ink ring-1 ring-rule'
								: 'text-ink-secondary hover:bg-surface-hover hover:text-ink'
						} ${collapsed ? 'justify-center px-0' : ''}`}
					>
						<item.icon class="size-4 shrink-0" aria-hidden="true" />
						<span class={collapsed ? 'sr-only' : 'truncate'}>{item.label}</span>
					</a>
				</li>
			{/each}
		</ul>

		<div class="shrink-0 border-t border-rule p-2">
			<button
				type="button"
				class="btn btn-ghost btn-sm w-full justify-start gap-2"
				aria-label={collapsed ? 'Expand navigation rail' : 'Collapse navigation rail'}
				onclick={() => (collapsed = !collapsed)}
			>
				{#if collapsed}
					<ChevronRight class="size-4" aria-hidden="true" />
				{:else}
					<ChevronLeft class="size-4" aria-hidden="true" />
					<span>Collapse</span>
				{/if}
			</button>
		</div>
	</div>

	<!-- Mobile rail: six 48px targets, two rows, nothing hidden. -->
	<ul class="grid h-[96px] grid-cols-3 md:hidden">
		{#each items as item (item.href)}
			<li class="min-w-0 border-t border-rule">
				<a
					href={item.href}
					aria-current={item.active ? 'page' : undefined}
					class={`flex h-full min-h-11 flex-col items-center justify-center gap-1 text-[12px] ${
						item.active ? 'bg-surface text-ink' : 'text-ink-secondary'
					}`}
				>
					<item.icon class="size-4" aria-hidden="true" />
					<span class="truncate">{item.label}</span>
				</a>
			</li>
		{/each}
	</ul>
</nav>
