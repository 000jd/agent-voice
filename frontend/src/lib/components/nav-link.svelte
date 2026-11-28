<script lang="ts">
	import type { Component } from 'svelte';
	import { cn } from '#lib/utils.js';

	let {
		href,
		label,
		icon: Icon,
		active,
		collapsed = false,
		onNavigate
	}: {
		href: string;
		label: string;
		icon: Component;
		active: boolean;
		collapsed?: boolean;
		onNavigate?: () => void;
	} = $props();
</script>

<a
	{href}
	aria-current={active ? 'page' : undefined}
	aria-label={collapsed ? label : undefined}
	title={collapsed ? label : undefined}
	onclick={() => onNavigate?.()}
	class={cn(
		'group relative flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-[13px] font-medium transition-colors focus-visible:ring-3 focus-visible:ring-ring/40 focus-visible:outline-none',
		collapsed && 'justify-center px-0',
		active
			? 'bg-elevated-hover text-foreground'
			: 'text-text-secondary hover:bg-elevated-hover/55 hover:text-foreground'
	)}
>
	{#if active && !collapsed}
		<span
			class="absolute top-2.5 bottom-2.5 left-0 w-0.5 rounded-full bg-primary"
			aria-hidden="true"
		></span>
	{/if}
	<Icon
		class={cn('size-4 shrink-0', active ? 'text-foreground' : 'text-text-muted')}
		aria-hidden="true"
	/>
	{#if !collapsed}
		<span class="truncate">{label}</span>
	{/if}
</a>
