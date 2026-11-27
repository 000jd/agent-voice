<script lang="ts" generics="T extends string">
	import { cn } from '#lib/utils.js';

	type Option = {
		value: T;
		label: string;
		disabled?: boolean;
		title?: string;
	};

	let {
		options,
		value,
		onchange,
		label,
		class: className
	}: {
		options: readonly Option[];
		value: T;
		onchange: (next: T) => void;
		label?: string;
		class?: string;
	} = $props();
</script>

<div
	class={cn('flex rounded-lg border border-border bg-panel p-0.5', className)}
	role="group"
	aria-label={label}
>
	{#each options as option (option.value)}
		<button
			type="button"
			class={cn(
				'flex h-7 min-w-0 flex-1 items-center justify-center rounded-md px-2 text-[11px] font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-40',
				value === option.value
					? 'bg-elevated-hover text-foreground'
					: 'text-text-secondary hover:text-foreground'
			)}
			aria-pressed={value === option.value}
			disabled={option.disabled}
			title={option.title}
			onclick={() => onchange(option.value)}
		>
			{option.label}
		</button>
	{/each}
</div>
