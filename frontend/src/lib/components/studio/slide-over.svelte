<script lang="ts">
	import { fade, fly } from 'svelte/transition';
	import X from '@lucide/svelte/icons/x';
	import type { Snippet } from 'svelte';

	let {
		open,
		title,
		description,
		onclose,
		children,
		footer
	}: {
		open: boolean;
		title: string;
		description?: string;
		onclose: () => void;
		children: Snippet;
		footer?: Snippet;
	} = $props();

	let panel = $state<HTMLElement | null>(null);
	let previousFocus: HTMLElement | null = null;

	$effect(() => {
		if (!open || typeof document === 'undefined') return;
		previousFocus = document.activeElement as HTMLElement | null;
		const previous = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		panel?.focus();
		return () => {
			document.body.style.overflow = previous;
			previousFocus?.focus();
		};
	});

	function onKeyDown(event: KeyboardEvent): void {
		if (!open) return;
		if (event.key === 'Escape') {
			onclose();
			return;
		}
		if (event.key !== 'Tab' || !panel) return;
		const focusable = Array.from(
			panel.querySelectorAll<HTMLElement>(
				'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
			)
		).filter((element) => element.offsetParent !== null);
		const first = focusable[0];
		const last = focusable[focusable.length - 1];
		if (!first || !last) return;
		if (event.shiftKey && document.activeElement === first) {
			event.preventDefault();
			last.focus();
		} else if (!event.shiftKey && document.activeElement === last) {
			event.preventDefault();
			first.focus();
		}
	}
</script>

<svelte:window onkeydown={onKeyDown} />

{#if open}
	<button
		type="button"
		class="fixed inset-0 z-50 bg-black/70"
		aria-label={`Close ${title}`}
		transition:fade={{ duration: 120 }}
		onclick={onclose}
	></button>
	<div
		bind:this={panel}
		class="fixed inset-y-0 right-0 z-50 flex w-full max-w-[420px] flex-col border-l border-border bg-panel shadow-2xl outline-none"
		role="dialog"
		aria-modal="true"
		aria-labelledby="studio-drawer-title"
		tabindex="-1"
		transition:fly={{ x: 28, duration: 180 }}
	>
		<header class="flex items-start justify-between gap-3 border-b border-border px-4 py-3.5">
			<div class="min-w-0">
				<h2 id="studio-drawer-title" class="text-[15px] font-semibold tracking-[-0.01em]">
					{title}
				</h2>
				{#if description}
					<p class="mt-0.5 text-[11px] leading-4 text-text-secondary">{description}</p>
				{/if}
			</div>
			<button
				type="button"
				class="flex size-7 shrink-0 items-center justify-center rounded-md text-text-muted hover:bg-elevated-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none"
				aria-label={`Close ${title}`}
				onclick={onclose}
			>
				<X class="size-4" aria-hidden="true" />
			</button>
		</header>
		<div class="min-h-0 flex-1 overflow-y-auto px-4 py-4">
			{@render children()}
		</div>
		{#if footer}
			<footer class="border-t border-border bg-panel px-4 py-3">
				{@render footer()}
			</footer>
		{/if}
	</div>
{/if}
