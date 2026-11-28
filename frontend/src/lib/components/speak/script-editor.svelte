<script lang="ts">
	import { tick } from 'svelte';
	import GenerationStatus from '#lib/components/speak/generation-status.svelte';
	import MarkupToolbar from '#lib/components/speak/markup-toolbar.svelte';
	import { insertMarkup, type MarkupAction } from '#lib/markup.js';
	import { modeSummary } from '#lib/speak.js';
	import { speak } from '#lib/stores/speak.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';

	let textarea = $state<HTMLTextAreaElement | null>(null);

	async function insert(action: MarkupAction): Promise<void> {
		const element = textarea;
		if (!element) return;
		const result = insertMarkup(speak.text, element.selectionStart, element.selectionEnd, action);
		speak.text = result.text;
		await tick();
		element.focus();
		element.setSelectionRange(result.selectionStart, result.selectionEnd);
	}

	function onInsert(action: MarkupAction): void {
		void insert(action);
	}

	function onKeyDown(event: KeyboardEvent): void {
		if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
			event.preventDefault();
			void speak.generate();
		}
	}
</script>

<div class="flex min-h-0 flex-1 flex-col">
	<div class="flex shrink-0 items-baseline justify-between gap-3 px-3 pt-3 md:px-4">
		<h2 class="text-[12px] font-medium text-ink-secondary">Script</h2>
		<p class="metric text-[11px] text-ink-muted">
			{speak.characterCount} / 20,000 chars · {speak.wordCount} words
		</p>
	</div>

	<div class="min-h-0 flex-1 overflow-y-auto px-3 md:px-4">
		<label class="sr-only" for="script-text">Speech text</label>
		<textarea
			id="script-text"
			class="min-h-[220px] w-full resize-none border-0 bg-transparent p-0 text-[20px] leading-[1.55] text-ink outline-none placeholder:text-ink-muted md:text-[22px]"
			bind:this={textarea}
			bind:value={speak.text}
			onkeydown={onKeyDown}
			spellcheck="true"
			placeholder="Write the line to speak. One sentence is a fine start."
			aria-describedby="script-hint"
			data-testid="script-text"></textarea>
	</div>

	<div class="shrink-0 border-t border-rule px-3 py-2 md:px-4">
		<MarkupToolbar disabled={!speak.config.markup} oninsert={onInsert} />

		<div class="mt-2 flex flex-wrap items-center gap-2">
			<button
				type="button"
				class="btn btn-primary"
				disabled={speak.busy || speak.validationError !== null}
				onclick={() => void speak.generate()}
			>
				{speak.busy ? 'Rendering…' : 'Generate speech'}
			</button>
			<span class="text-[11px] text-ink-muted">Ctrl/⌘ + Enter</span>
			<span
				id="script-hint"
				class="min-w-0 flex-1 truncate text-right text-[11px] text-ink-secondary"
			>
				{modeSummary(speak.config, studio.engines)}
			</span>
		</div>

		{#if !speak.config.markup}
			<p class="mt-1 text-[11px] text-ink-muted">
				Markup is off: bracketed tags are spoken literally for this take.
			</p>
		{/if}
		{#if speak.validationError}
			<p class="mt-1 text-[12px] text-warning" role="status">{speak.validationError}</p>
		{/if}

		<div class="mt-2">
			<GenerationStatus />
		</div>
	</div>
</div>
