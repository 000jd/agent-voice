<script lang="ts">
	import { onMount } from 'svelte';
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import Power from '@lucide/svelte/icons/power';
	import Search from '@lucide/svelte/icons/search';
	import { describeApiError } from '#lib/api/studio.js';
	import type { ApiErrorPayload } from '#lib/api/types.js';
	import EmptyState from '#lib/components/empty-state.svelte';
	import PageHeader from '#lib/components/page-header.svelte';
	import EngineCard from '#lib/components/studio/engine-card.svelte';
	import ErrorNotice from '#lib/components/studio/error-notice.svelte';
	import { Button } from '#lib/components/ui/button/index.js';
	import { formatGpuLabel } from '#lib/format.js';
	import { studio } from '#lib/stores/studio.svelte.js';

	type KindFilter = 'all' | 'tts' | 'asr';

	let query = $state('');
	let filter = $state<KindFilter>('all');
	let unloading = $state(false);
	let error = $state<ApiErrorPayload | string>('');
	let notice = $state('');

	const loaded = $derived(
		Boolean(
			studio.gpu?.activeEngine || studio.gpu?.loadedModels.length || studio.gpu?.cpuEngines.length
		)
	);
	const filtered = $derived(
		studio.engines.filter((engine) => {
			if (filter !== 'all' && engine.kind !== filter) return false;
			if (!query.trim()) return true;
			const needle = query.toLowerCase();
			return (
				engine.id.toLowerCase().includes(needle) ||
				engine.displayName.toLowerCase().includes(needle) ||
				engine.backend.toLowerCase().includes(needle) ||
				engine.models.some((model) => model.alias.toLowerCase().includes(needle))
			);
		})
	);
	const tts = $derived(filtered.filter((engine) => engine.kind === 'tts'));
	const asr = $derived(filtered.filter((engine) => engine.kind === 'asr'));

	onMount(() => {
		if (studio.engines.length === 0) void studio.refreshCatalog();
		void studio.refreshGpu();
	});

	async function unload(): Promise<void> {
		unloading = true;
		error = '';
		notice = '';
		try {
			const result = await studio.unloadAll();
			notice = `Unloaded ${result.unloaded.length} worker${result.unloaded.length === 1 ? '' : 's'}${
				result.cancelledJobs
					? ` · cancelled ${result.cancelledJobs} job${result.cancelledJobs === 1 ? '' : 's'}`
					: ''
			}.`;
		} catch (cause) {
			error = describeApiError(cause);
		} finally {
			unloading = false;
		}
	}
</script>

<PageHeader
	title="Models & Engines"
	description="Installed checkpoints, local weights, devices and live memory."
>
	<Button variant="outline" size="sm" disabled={!loaded || unloading} onclick={() => void unload()}>
		{#if unloading}
			<LoaderCircle class="size-3.5 animate-spin" aria-hidden="true" />
		{:else}
			<Power class="size-3.5" aria-hidden="true" />
		{/if}
		Unload all GPU models
	</Button>
</PageHeader>

{#if error}
	<div class="mb-3"><ErrorNotice {error} /></div>
{/if}
{#if notice}
	<p
		class="mb-3 rounded-lg border border-success/25 bg-success/10 px-3 py-2 text-[11px] text-success"
		role="status"
	>
		{notice}
	</p>
{/if}

<div class="mb-4 flex flex-col gap-2.5 sm:flex-row sm:items-center sm:justify-between">
	<div class="relative w-full sm:max-w-sm">
		<Search
			class="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-text-muted"
			aria-hidden="true"
		/>
		<input
			class="h-9 w-full rounded-lg border border-border bg-elevated pr-2.5 pl-8 text-[12px] text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
			placeholder="Search engines or models"
			bind:value={query}
			aria-label="Search engines or models"
		/>
	</div>
	<div class="flex items-center gap-1" role="group" aria-label="Engine kind filter">
		{#each [{ value: 'all', label: 'All' }, { value: 'tts', label: 'TTS' }, { value: 'asr', label: 'ASR' }] as option (option.value)}
			<button
				type="button"
				class={`h-7 rounded-full border px-3 text-[11px] font-medium transition-colors ${
					filter === option.value
						? 'border-border bg-elevated-hover text-foreground'
						: 'border-transparent text-text-secondary hover:bg-elevated-hover/50 hover:text-foreground'
				}`}
				aria-pressed={filter === option.value}
				onclick={() => (filter = option.value as KindFilter)}
			>
				{option.label}
			</button>
		{/each}
	</div>
	<span class="font-mono text-[10px] text-text-muted tabular-nums">
		{formatGpuLabel(studio.gpu)}
	</span>
</div>

{#if studio.catalogLoading && studio.engines.length === 0}
	<div class="grid gap-3 lg:grid-cols-2">
		{#each [0, 1, 2, 3] as card (card)}
			<div class="h-[220px] animate-pulse rounded-xl border border-border bg-elevated"></div>
		{/each}
	</div>
{:else if studio.catalogError && studio.engines.length === 0}
	<ErrorNotice error={studio.catalogError} onRetry={() => void studio.refreshCatalog()} />
{:else if filtered.length === 0}
	<EmptyState
		icon={Power}
		title="No engines match"
		description="Clear the search or switch the TTS/ASR filter. Engines are discovered from engines/*/engine.yaml."
	/>
{:else}
	{#if tts.length > 0}
		<section class="mb-5">
			<h2 class="mb-2 text-[11px] font-medium text-text-muted">TTS engines</h2>
			<div class="divide-y divide-border overflow-hidden rounded-xl border border-border bg-panel">
				{#each tts as engine (engine.id)}
					<EngineCard {engine} />
				{/each}
			</div>
		</section>
	{/if}
	{#if asr.length > 0}
		<section>
			<h2 class="mb-2 text-[11px] font-medium text-text-muted">ASR engines</h2>
			<div class="divide-y divide-border overflow-hidden rounded-xl border border-border bg-panel">
				{#each asr as engine (engine.id)}
					<EngineCard {engine} />
				{/each}
			</div>
		</section>
	{/if}
{/if}
