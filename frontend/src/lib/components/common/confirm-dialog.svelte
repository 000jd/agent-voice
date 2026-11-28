<script lang="ts">
	import Dialog from './dialog.svelte';

	let {
		open = $bindable(false),
		title,
		message,
		detail = '',
		confirmLabel = 'Confirm',
		danger = false,
		onconfirm,
		oncancel
	}: {
		open?: boolean;
		title: string;
		message: string;
		detail?: string;
		confirmLabel?: string;
		danger?: boolean;
		onconfirm: () => void;
		oncancel?: () => void;
	} = $props();
</script>

<Dialog bind:open {title} size="sm">
	<div class="space-y-2">
		<p class="text-[13px] leading-6 text-ink">{message}</p>
		{#if detail}
			<p class="text-[12px] leading-5 text-ink-secondary">{detail}</p>
		{/if}
	</div>
	{#snippet footer()}
		<button
			type="button"
			class="btn"
			onclick={() => {
				open = false;
				oncancel?.();
			}}
		>
			Cancel
		</button>
		<button
			type="button"
			class={`btn ${danger ? 'btn-danger' : 'btn-primary'}`}
			onclick={() => {
				open = false;
				onconfirm();
			}}
		>
			{confirmLabel}
		</button>
	{/snippet}
</Dialog>
