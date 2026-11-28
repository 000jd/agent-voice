<script lang="ts">
	import { Dialog } from 'bits-ui';
	import X from '@lucide/svelte/icons/x';
	import type { Snippet } from 'svelte';

	let {
		open = $bindable(false),
		title,
		description = '',
		size = 'md',
		fullMobile = false,
		onclose,
		children,
		footer
	}: {
		open?: boolean;
		title: string;
		description?: string;
		size?: 'sm' | 'md' | 'lg';
		fullMobile?: boolean;
		onclose?: () => void;
		children: Snippet;
		footer?: Snippet;
	} = $props();

	const widths: Record<'sm' | 'md' | 'lg', string> = {
		sm: 'sm:max-w-[420px]',
		md: 'sm:max-w-[560px]',
		lg: 'sm:max-w-[720px]'
	};
</script>

<Dialog.Root
	bind:open
	onOpenChange={(next) => {
		if (!next) onclose?.();
	}}
>
	<Dialog.Portal>
		<Dialog.Overlay class="fixed inset-0 z-50 bg-scrim" />
		<Dialog.Content
			class={`fixed inset-x-3 top-1/2 z-50 flex -translate-y-1/2 flex-col overflow-hidden rounded-panel border border-rule-strong bg-panel sm:inset-x-auto sm:left-1/2 sm:w-[calc(100vw-48px)] sm:-translate-x-1/2 ${widths[size]} ${
				fullMobile ? 'max-h-[calc(100dvh-24px)]' : 'max-h-[calc(100dvh-32px)]'
			}`}
		>
			<header
				class="flex shrink-0 items-start justify-between gap-4 border-b border-rule px-5 py-3.5"
			>
				<div class="min-w-0">
					<Dialog.Title class="text-[15px] leading-6 font-semibold text-ink">{title}</Dialog.Title>
					{#if description}
						<Dialog.Description class="mt-0.5 text-[12px] leading-5 text-ink-secondary">
							{description}
						</Dialog.Description>
					{:else}
						<Dialog.Description class="sr-only">{title}</Dialog.Description>
					{/if}
				</div>
				<button
					type="button"
					class="btn btn-ghost btn-icon shrink-0"
					aria-label="Close dialog"
					onclick={() => {
						open = false;
						onclose?.();
					}}
				>
					<X class="size-4" aria-hidden="true" />
				</button>
			</header>
			<div class="min-h-0 flex-1 overflow-y-auto px-5 py-4">
				{@render children()}
			</div>
			{#if footer}
				<footer
					class="flex shrink-0 items-center justify-end gap-2 border-t border-rule bg-panel px-5 py-3"
				>
					{@render footer()}
				</footer>
			{/if}
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
