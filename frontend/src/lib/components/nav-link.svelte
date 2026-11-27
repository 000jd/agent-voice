<script lang="ts">
	import type { Component } from 'svelte';
	import { cn } from '#lib/utils.js';

	let {
		href,
		label,
		icon: Icon,
		active
	}: {
		href: string;
		label: string;
		icon: Component;
		active: boolean;
	} = $props();
</script>

<a
	{href}
	aria-current={active ? 'page' : undefined}
	class={cn(
		'group flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
		active
			? 'bg-sidebar-accent text-sidebar-accent-foreground'
			: 'text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground'
	)}
>
	<Icon class="size-4 shrink-0" aria-hidden="true" />
	<span>{label}</span>
</a>
