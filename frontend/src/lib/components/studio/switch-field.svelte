<script lang="ts">
	import { cn } from '#lib/utils.js';

	let {
		checked,
		onchange,
		label,
		description,
		disabled = false
	}: {
		checked: boolean;
		onchange: (checked: boolean) => void;
		label: string;
		description?: string;
		disabled?: boolean;
	} = $props();
</script>

<button
	type="button"
	role="switch"
	aria-checked={checked}
	{disabled}
	class="flex min-h-10 w-full items-center justify-between gap-3 rounded-lg px-1 py-1 text-left focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none disabled:opacity-50"
	onclick={() => onchange(!checked)}
>
	<span class="min-w-0">
		<span class="block text-[12px] font-medium text-foreground">{label}</span>
		{#if description}
			<span class="mt-0.5 block text-[10px] leading-4 text-text-muted">{description}</span>
		{/if}
	</span>
	<span
		class={cn(
			'relative h-4 w-7 shrink-0 rounded-full transition-colors',
			checked ? 'bg-primary' : 'bg-elevated-hover'
		)}
		aria-hidden="true"
	>
		<span
			class={cn(
				'absolute top-0.5 size-3 rounded-full bg-white transition-transform',
				checked ? 'left-3.5' : 'left-0.5'
			)}
		></span>
	</span>
</button>
