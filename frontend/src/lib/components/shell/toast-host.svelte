<script lang="ts">
	import X from '@lucide/svelte/icons/x';
	import { toasts } from '#lib/stores/toasts.svelte.js';
</script>

<div
	class="pointer-events-none fixed right-4 bottom-4 z-40 flex w-[min(360px,calc(100vw-32px))] flex-col gap-2"
	aria-live="polite"
	role="status"
>
	{#each toasts.items as toast (toast.id)}
		<div
			class={`pointer-events-auto flex items-start gap-2 rounded-panel border px-3 py-2 ${
				toast.kind === 'error'
					? 'border-danger bg-surface'
					: toast.kind === 'success'
						? 'border-success bg-surface'
						: 'border-rule-strong bg-surface'
			}`}
		>
			<div class="min-w-0 flex-1">
				<p class="text-[12px] leading-5 font-medium text-ink">{toast.message}</p>
				{#if toast.detail}
					<p class="text-[11px] leading-4 text-ink-secondary">{toast.detail}</p>
				{/if}
			</div>
			<button
				type="button"
				class="btn btn-ghost btn-icon shrink-0"
				aria-label="Dismiss notification"
				onclick={() => toasts.dismiss(toast.id)}
			>
				<X class="size-3.5" aria-hidden="true" />
			</button>
		</div>
	{/each}
</div>
