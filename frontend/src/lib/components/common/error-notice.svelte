<script lang="ts">
	import CircleAlert from '@lucide/svelte/icons/circle-alert';
	import type { ApiErrorPayload } from '#lib/api/types.js';

	let {
		error,
		onRetry = null,
		retryLabel = 'Retry',
		compact = false
	}: {
		error: ApiErrorPayload;
		onRetry?: (() => void) | null;
		retryLabel?: string;
		compact?: boolean;
	} = $props();
</script>

<div
	class={`flex items-start gap-2.5 rounded-panel border border-rule bg-surface ${compact ? 'px-3 py-2' : 'px-4 py-3'}`}
	role="alert"
>
	<CircleAlert class="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
	<div class="min-w-0 flex-1">
		<p class="text-[12px] leading-5 font-medium text-ink">{error.message}</p>
		{#if error.suggestion}
			<p class="mt-0.5 text-[12px] leading-5 text-ink-secondary">{error.suggestion}</p>
		{/if}
		{#if error.correlationId}
			<p class="metric mt-1 text-[10px] text-ink-muted">ref {error.correlationId}</p>
		{/if}
	</div>
	{#if onRetry}
		<button type="button" class="btn btn-sm shrink-0" onclick={onRetry}>{retryLabel}</button>
	{/if}
</div>
