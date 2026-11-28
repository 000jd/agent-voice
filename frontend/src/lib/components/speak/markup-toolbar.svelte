<script lang="ts">
	import { MARKUP_ACTIONS, type MarkupAction } from '#lib/markup.js';

	let {
		disabled = false,
		oninsert
	}: { disabled?: boolean; oninsert: (action: MarkupAction) => void } = $props();
</script>

<div class="flex flex-wrap items-center gap-1" role="group" aria-label="Expressive markup">
	<span class="mr-1 text-[11px] text-ink-muted">Markup</span>
	{#each MARKUP_ACTIONS as action (action.id)}
		<button
			type="button"
			class="btn btn-ghost btn-sm metric h-6 px-1.5 text-[11px]"
			{disabled}
			title={`Insert ${action.snippet}`}
			onclick={() => oninsert(action)}
		>
			{action.label}
		</button>
	{/each}
	<span class="ml-1 text-[11px] text-ink-muted">[slow] [fast] [emphasis] [spell] [pause 500ms]</span
	>
</div>
