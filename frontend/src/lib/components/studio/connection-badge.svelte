<script lang="ts">
	type Connection = 'checking' | 'connected' | 'offline';

	let { state, compact = false }: { state: Connection; compact?: boolean } = $props();

	const label = $derived(
		state === 'connected' ? 'Connected' : state === 'offline' ? 'Offline' : 'Connecting'
	);
	const dot = $derived(
		state === 'connected' ? 'bg-success' : state === 'offline' ? 'bg-danger' : 'bg-text-muted'
	);
</script>

<span
	class="inline-flex h-7 items-center gap-1.5 rounded-full border border-border px-2.5 text-[11px] font-medium text-text-secondary"
	role="status"
	aria-label={`Daemon ${label.toLowerCase()}`}
	title={`Daemon ${label.toLowerCase()}`}
>
	<span class={`size-1.5 rounded-full ${dot}`} aria-hidden="true"></span>
	{#if !compact}
		<span>{label}</span>
	{/if}
</span>
