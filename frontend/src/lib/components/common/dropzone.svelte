<script lang="ts">
	import Upload from '@lucide/svelte/icons/upload';

	let {
		id,
		label,
		hint = '',
		accept = 'audio/*',
		disabled = false,
		compact = false,
		onfile
	}: {
		id: string;
		label: string;
		hint?: string;
		accept?: string;
		disabled?: boolean;
		compact?: boolean;
		onfile: (file: File) => void;
	} = $props();

	let dragging = $state(false);

	function take(file: File | undefined | null): void {
		if (!file || disabled) return;
		onfile(file);
	}

	function onDrop(event: DragEvent): void {
		event.preventDefault();
		dragging = false;
		if (disabled) return;
		const file = event.dataTransfer?.files?.[0];
		take(file ?? null);
	}
</script>

<div
	class={`rounded-panel border border-dashed transition-colors ${
		dragging ? 'border-signal bg-signal-soft' : 'border-rule-strong bg-surface'
	} ${disabled ? 'opacity-60' : ''}`}
	role="group"
	aria-label={label}
	ondragover={(event) => {
		event.preventDefault();
		if (!disabled) dragging = true;
	}}
	ondragleave={() => (dragging = false)}
	ondrop={onDrop}
>
	<label
		for={id}
		class={`flex cursor-pointer flex-col items-center justify-center rounded-panel text-center focus-within:ring-2 focus-within:ring-signal ${
			compact ? 'gap-1 px-3 py-3' : 'gap-2 px-4 py-7'
		}`}
	>
		<Upload class={`${compact ? 'size-4' : 'size-5'} text-ink-muted`} aria-hidden="true" />
		<span class="text-[13px] font-medium text-ink">{label}</span>
		{#if hint}
			<span class="max-w-[44ch] text-[11px] leading-4 text-ink-secondary">{hint}</span>
		{/if}
		<input
			{id}
			class="sr-only"
			type="file"
			{accept}
			{disabled}
			onchange={(event) => {
				take(event.currentTarget.files?.[0] ?? null);
				event.currentTarget.value = '';
			}}
		/>
	</label>
</div>
