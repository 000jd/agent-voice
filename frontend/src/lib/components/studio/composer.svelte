<script lang="ts">
	import { tick } from 'svelte';
	import AudioLines from '@lucide/svelte/icons/audio-lines';
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import { Button } from '#lib/components/ui/button/index.js';
	import { insertMarkup, MARKUP_ACTIONS, type MarkupAction } from '#lib/markup.js';

	let {
		value = $bindable(''),
		onGenerate,
		generating = false,
		error = '',
		maxChars = 20000
	}: {
		value?: string;
		onGenerate: () => void;
		generating?: boolean;
		error?: string;
		maxChars?: number;
	} = $props();

	let textarea = $state<HTMLTextAreaElement | null>(null);

	async function insert(action: MarkupAction): Promise<void> {
		const start = textarea?.selectionStart ?? value.length;
		const end = textarea?.selectionEnd ?? value.length;
		const result = insertMarkup(value, start, end, action);
		value = result.text;
		await tick();
		textarea?.focus();
		textarea?.setSelectionRange(result.selectionStart, result.selectionEnd);
	}

	function onKeyDown(event: KeyboardEvent): void {
		if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
			event.preventDefault();
			if (!generating && value.trim()) onGenerate();
		}
	}
</script>

<div
	class="studio-texture flex min-h-[560px] flex-col overflow-hidden rounded-xl border border-border bg-elevated transition-colors focus-within:border-ring/60 xl:min-h-[calc(100dvh-250px)]"
>
	<div class="flex flex-wrap items-center gap-1 border-b border-border/60 px-4 py-2.5">
		<span class="mr-1 text-[11px] text-text-muted">Insert</span>
		{#each MARKUP_ACTIONS as action (action.id)}
			<button
				type="button"
				class="h-7 rounded-md px-2 font-mono text-[10px] text-text-muted transition-colors hover:bg-panel hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none"
				title={`Insert ${action.snippet}`}
				onclick={() => void insert(action)}
			>
				{action.snippet}
			</button>
		{/each}
	</div>

	<textarea
		bind:this={textarea}
		bind:value
		class="min-h-[360px] w-full flex-1 resize-none bg-transparent px-6 py-6 text-[24px] leading-[1.55] tracking-[-0.01em] text-foreground placeholder:text-text-muted focus:outline-none md:text-[27px]"
		placeholder="Write the line you want to hear."
		spellcheck="true"
		aria-label="Speech text"
		maxlength={maxChars}
		onkeydown={onKeyDown}></textarea>

	{#if error}
		<div
			class="mx-4 mb-3 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-[11px] leading-4 text-danger"
			aria-live="polite"
		>
			{error}
		</div>
	{/if}

	<div
		class="flex flex-wrap items-center justify-between gap-3 border-t border-border/60 px-4 py-3"
	>
		<div class="flex flex-wrap items-center gap-3">
			<span class="font-mono text-[11px] text-text-muted tabular-nums">
				{value.length.toLocaleString()} / {maxChars.toLocaleString()}
			</span>
			<span class="text-[11px] text-text-muted">Ctrl/Cmd + Enter to generate</span>
		</div>
		<Button
			class="h-10 rounded-xl px-5"
			disabled={generating || value.trim().length === 0}
			onclick={onGenerate}
		>
			{#if generating}
				<LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
				Generating
			{:else}
				<AudioLines class="size-4" aria-hidden="true" />
				Generate speech
			{/if}
		</Button>
	</div>
</div>
