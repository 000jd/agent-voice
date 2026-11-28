<script lang="ts">
	import { onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import { describeApiError, speak } from '#lib/api/studio.js';
	import type { ApiErrorPayload } from '#lib/api/types.js';
	import Composer from '#lib/components/studio/composer.svelte';
	import ErrorNotice from '#lib/components/studio/error-notice.svelte';
	import SpeakInspector from '#lib/components/studio/speak-inspector.svelte';
	import { buildSpeakRequest, validateSpeak } from '#lib/speak.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';

	const player = getPlayer();
	let text = $state('');
	let generating = $state(false);
	let error = $state<ApiErrorPayload | string>('');

	onMount(() => {
		if (studio.engines.length === 0) void studio.refreshCatalog();
		else studio.ensureSpeakConfig();
	});

	async function generate(): Promise<void> {
		const validation = validateSpeak(text, studio.speak);
		if (validation) {
			error = validation;
			return;
		}
		generating = true;
		error = '';
		try {
			const result = await speak(buildSpeakRequest(text, studio.speak));
			const clip = studio.addClip(result, text);
			await player.play(clip);
		} catch (cause) {
			error = describeApiError(cause);
		} finally {
			generating = false;
		}
	}
</script>

<div class="mb-4 flex items-center justify-between gap-3">
	<h1 class="text-[14px] font-medium text-text-primary">Speak</h1>
	<a
		href={resolve('voices')}
		class="text-[11px] text-text-secondary underline-offset-4 hover:text-foreground hover:underline"
		>Manage voices</a
	>
</div>

{#if studio.catalogLoading && studio.engines.length === 0}
	<div class="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
		<div class="h-[520px] animate-pulse rounded-xl border border-border bg-elevated"></div>
		<div class="h-[520px] animate-pulse rounded-xl border border-border bg-elevated"></div>
	</div>
{:else if studio.catalogError && studio.engines.length === 0}
	<ErrorNotice error={studio.catalogError} onRetry={() => void studio.refreshCatalog()} />
{:else}
	<div class="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
		<div class="min-w-0">
			{#if error}
				<ErrorNotice {error} />
			{/if}
			<Composer bind:value={text} onGenerate={() => void generate()} {generating} error="" />
		</div>
		<div class="min-w-0 xl:sticky xl:top-[68px] xl:max-h-[calc(100dvh-260px)] xl:overflow-y-auto">
			<SpeakInspector />
		</div>
	</div>
{/if}
