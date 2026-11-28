<script lang="ts">
	import type { SettingEntry } from '#lib/api/types.js';

	let {
		entry,
		value,
		onchange
	}: {
		entry: SettingEntry;
		value: unknown;
		onchange: (next: unknown) => void;
	} = $props();

	const display = $derived(
		typeof value === 'string'
			? value
			: typeof value === 'boolean'
				? value
					? 'true'
					: 'false'
				: Array.isArray(value)
					? value.join(', ')
					: value === null || value === undefined
						? '—'
						: JSON.stringify(value)
	);
	const id = $derived(`setting-${entry.key.toLowerCase()}`);
</script>

{#if entry.kind === 'bool'}
	<label class="flex items-start gap-2">
		<input
			class="checkbox mt-0.5"
			type="checkbox"
			checked={value === true}
			onchange={(event) => onchange(event.currentTarget.checked)}
		/>
		<span class="min-w-0">
			<span class="block text-[12px] font-medium text-ink">{entry.label}</span>
			<span class="metric block truncate text-[11px] text-ink-muted">{entry.key}</span>
		</span>
	</label>
{:else}
	<div class="space-y-1">
		{#if entry.kind === 'readonly'}
			<p class="field-label">{entry.label}</p>
		{:else}
			<label class="field-label" for={id}>{entry.label}</label>
		{/if}

		{#if entry.kind === 'readonly'}
			<p class="metric truncate text-[12px] text-ink" title={display}>{display}</p>
		{:else if entry.kind === 'select'}
			<select
				{id}
				class="select h-8 text-[12px]"
				value={String(display)}
				onchange={(event) => onchange(event.currentTarget.value)}
			>
				{#each entry.choices as choice (choice)}
					<option value={choice}>{choice}</option>
				{/each}
			</select>
		{:else if entry.kind === 'int' || entry.kind === 'float'}
			<input
				{id}
				class="input metric h-8 text-[12px]"
				type="number"
				step={entry.kind === 'int' ? '1' : '0.1'}
				value={String(display)}
				onchange={(event) => onchange(Number(event.currentTarget.value))}
			/>
		{:else}
			<input
				{id}
				class="input h-8 text-[12px]"
				type="text"
				value={String(display)}
				onchange={(event) => onchange(event.currentTarget.value)}
			/>
		{/if}

		<p class="text-[11px] leading-4 text-ink-muted">
			{#if entry.description}{entry.description} ·
			{/if}
			<span class="metric">{entry.key}</span>
			{#if entry.source === 'env'}· from the environment{/if}
			{#if entry.kind === 'readonly'}· read-only here{/if}
		</p>
	</div>
{/if}
