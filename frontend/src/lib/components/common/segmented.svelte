<script lang="ts" module>
	export type Segment = { id: string; label: string; count?: number };
</script>

<script lang="ts">
	let {
		items,
		value,
		onchange,
		label
	}: {
		items: readonly Segment[];
		value: string;
		onchange: (id: string) => void;
		label: string;
	} = $props();
</script>

<div
	role="group"
	aria-label={label}
	class="inline-flex max-w-full flex-wrap items-center gap-0.5 rounded-control border border-rule bg-panel p-0.5"
>
	{#each items as item (item.id)}
		<button
			type="button"
			aria-pressed={value === item.id}
			class={`h-7 shrink-0 rounded-[4px] px-2.5 text-[12px] whitespace-nowrap transition-colors ${
				value === item.id
					? 'bg-surface font-medium text-ink shadow-none ring-1 ring-rule'
					: 'text-ink-secondary hover:bg-surface-hover hover:text-ink'
			}`}
			onclick={() => onchange(item.id)}
		>
			{item.label}
			{#if item.count !== undefined}
				<span class="metric ml-1 text-[10px] text-ink-muted">{item.count}</span>
			{/if}
		</button>
	{/each}
</div>
