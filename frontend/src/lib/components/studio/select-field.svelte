<script lang="ts">
	import ChevronDown from '@lucide/svelte/icons/chevron-down';

	type Option = { value: string; label: string; disabled?: boolean };

	let {
		id,
		label,
		value,
		options,
		onchange,
		disabled = false,
		hint
	}: {
		id: string;
		label: string;
		value: string;
		options: readonly Option[];
		onchange: (value: string) => void;
		disabled?: boolean;
		hint?: string;
	} = $props();
</script>

<div class="min-w-0">
	<label
		for={id}
		class="mb-1 block text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase"
	>
		{label}
	</label>
	<div class="relative">
		<select
			{id}
			class="h-8 w-full appearance-none rounded-lg border border-border bg-elevated px-2.5 pr-7 text-[12px] text-foreground transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50"
			{value}
			{disabled}
			aria-describedby={hint ? `${id}-hint` : undefined}
			onchange={(event) => onchange(event.currentTarget.value)}
		>
			{#each options as option (option.value)}
				<option value={option.value} disabled={option.disabled}>{option.label}</option>
			{/each}
		</select>
		<ChevronDown
			class="pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2 text-text-muted"
			aria-hidden="true"
		/>
	</div>
	{#if hint}
		<p id={`${id}-hint`} class="mt-1 text-[10px] leading-4 text-text-muted">{hint}</p>
	{/if}
</div>
