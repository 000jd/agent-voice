<script lang="ts">
	import { onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import { describeApiError, speak } from '#lib/api/studio.js';
	import type { ApiErrorPayload } from '#lib/api/types.js';
	import PageHeader from '#lib/components/page-header.svelte';
	import Composer from '#lib/components/studio/composer.svelte';
	import ErrorNotice from '#lib/components/studio/error-notice.svelte';
	import SpeakInspector from '#lib/components/studio/speak-inspector.svelte';
	import { Button } from '#lib/components/ui/button/index.js';
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

<PageHeader
	title="Speak"
	description="Compose one line or a scene, shape the delivery, and keep the takes in this session."
>
	<Button href={resolve('voices')} variant="outline" size="sm">Manage voices</Button>
</PageHeader>

{#if studio.catalogLoading && studio.engines.length === 0}
	<div class="grid gap-4 xl:grid-cols-[minmax(0,1fr)_300px]">
		<div class="h-[420px] animate-pulse rounded-xl border border-border bg-elevated"></div>
		<div class="h-[420px] animate-pulse rounded-xl border border-border bg-elevated"></div>
	</div>
{:else if studio.catalogError && studio.engines.length === 0}
	<ErrorNotice error={studio.catalogError} onRetry={() => void studio.refreshCatalog()} />
{:else}
	<div class="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_300px]">
		<div class="min-w-0 space-y-3">
			{#if error}
				<ErrorNotice {error} />
			{/if}
			<Composer bind:value={text} onGenerate={() => void generate()} {generating} error="" />
			<p class="px-1 text-[10px] leading-4 text-text-muted">
				Audio is fetched same-origin from the local daemon and never uploaded anywhere. If the
				daemon has an API key set, the static UI cannot store it; see Settings for the dev-proxy
				note.
			</p>
		</div>
		<div
			class="min-w-0 xl:sticky xl:top-[68px] xl:max-h-[calc(100dvh-160px)] xl:overflow-y-auto xl:pr-1"
		>
			<SpeakInspector />
		</div>
	</div>
{/if}
