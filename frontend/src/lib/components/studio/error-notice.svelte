<script lang="ts">
	import { Button } from '#lib/components/ui/button/index.js';
	import type { ApiErrorPayload } from '#lib/api/types.js';

	let {
		error,
		onRetry,
		retryLabel = 'Retry'
	}: {
		error: ApiErrorPayload | string;
		onRetry?: () => void;
		retryLabel?: string;
	} = $props();

	const detail = $derived(typeof error === 'string' ? null : error);
</script>

<div
	class="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-[11px] leading-4"
	role="alert"
>
	<div class="flex flex-wrap items-start justify-between gap-2">
		<div class="min-w-0">
			{#if detail}
				<p class="font-mono text-[10px] text-destructive">
					{detail.code}{detail.status ? ` · ${detail.status}` : ''}
				</p>
				<p class="mt-0.5 text-foreground">{detail.message}</p>
				{#if detail.suggestion}
					<p class="mt-0.5 text-text-secondary">{detail.suggestion}</p>
				{/if}
			{:else}
				<p class="text-foreground">{error}</p>
			{/if}
		</div>
		{#if onRetry}
			<Button variant="outline" size="xs" onclick={onRetry}>{retryLabel}</Button>
		{/if}
	</div>
</div>
