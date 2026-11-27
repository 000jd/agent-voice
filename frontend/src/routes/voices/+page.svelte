<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import Mic from '@lucide/svelte/icons/mic';
	import Plus from '@lucide/svelte/icons/plus';
	import Search from '@lucide/svelte/icons/search';
	import Sparkles from '@lucide/svelte/icons/sparkles';
	import { describeApiError } from '#lib/api/studio.js';
	import type { ApiErrorPayload } from '#lib/api/types.js';
	import EmptyState from '#lib/components/empty-state.svelte';
	import PageHeader from '#lib/components/page-header.svelte';
	import CloneDrawer from '#lib/components/studio/clone-drawer.svelte';
	import DesignDrawer from '#lib/components/studio/design-drawer.svelte';
	import ErrorNotice from '#lib/components/studio/error-notice.svelte';
	import PresetGroupCard from '#lib/components/studio/preset-group-card.svelte';
	import VoiceCard from '#lib/components/studio/voice-card.svelte';
	import { Button } from '#lib/components/ui/button/index.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import {
		builtinPresetGroups,
		filterPresetGroups,
		filterVoiceItems,
		voiceItems,
		type VoiceFilter
	} from '#lib/voices.js';

	let query = $state('');
	let filter = $state<VoiceFilter>('all');
	let designOpen = $state(false);
	let cloneOpen = $state(false);
	let error = $state<ApiErrorPayload | string>('');

	const items = $derived(voiceItems(studio.voices, []));
	const groups = $derived(builtinPresetGroups(studio.engines));
	const filtered = $derived(filterVoiceItems(items, filter, query));
	const filteredGroups = $derived(
		filter === 'preset' || filter === 'all' ? filterPresetGroups(groups, query) : []
	);
	const visibleCount = $derived(filtered.length + filteredGroups.length);
	const filters: readonly { value: VoiceFilter; label: string }[] = [
		{ value: 'all', label: 'All' },
		{ value: 'cloned', label: 'Cloned' },
		{ value: 'designed', label: 'Designed' },
		{ value: 'preset', label: 'Built-in presets' }
	];

	function filterCount(option: VoiceFilter): number {
		if (option === 'all') return items.length + groups.length;
		if (option === 'preset') return groups.length;
		return items.filter((item) => item.kind === 'voice' && item.voice.source === option).length;
	}

	$effect(() => {
		if (page.url.searchParams.get('design') !== '1') return;
		designOpen = true;
		void goto(resolve('voices'), { replaceState: true });
	});

	onMount(() => {
		if (studio.engines.length === 0) void studio.refreshCatalog();
	});

	async function remove(voiceId: string): Promise<void> {
		error = '';
		try {
			await studio.removeVoice(voiceId);
		} catch (cause) {
			error = describeApiError(cause);
			throw cause;
		}
	}

	function saved(voiceId: string): void {
		filter = 'designed';
		query = '';
		error = '';
		void voiceId;
	}
</script>

<PageHeader
	title="Voices"
	description="Reusable cloned and designed timbres, plus the preset speakers each engine ships."
>
	<Button variant="outline" size="sm" onclick={() => (cloneOpen = true)}>
		<Plus class="size-3.5" aria-hidden="true" />
		Clone voice
	</Button>
	<Button size="sm" onclick={() => (designOpen = true)}>
		<Sparkles class="size-3.5" aria-hidden="true" />
		Design voice
	</Button>
</PageHeader>

{#if error}
	<div class="mb-3">
		<ErrorNotice {error} />
	</div>
{/if}

<div class="mb-4 flex flex-col gap-2.5 lg:flex-row lg:items-center lg:justify-between">
	<div class="relative w-full lg:max-w-sm">
		<Search
			class="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-text-muted"
			aria-hidden="true"
		/>
		<input
			class="h-8 w-full rounded-lg border border-border bg-elevated pr-2.5 pl-8 text-[12px] text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
			placeholder="Search voices"
			bind:value={query}
			aria-label="Search voices"
		/>
	</div>
	<div class="flex gap-1 overflow-x-auto pb-0.5" role="group" aria-label="Voice filters">
		{#each filters as option (option.value)}
			<button
				type="button"
				class={`inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full border px-2.5 text-[11px] font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none ${
					filter === option.value
						? 'border-primary/30 bg-primary/10 text-primary'
						: 'border-border bg-elevated text-text-secondary hover:text-foreground'
				}`}
				aria-pressed={filter === option.value}
				onclick={() => (filter = option.value)}
			>
				{option.label}
				<span class="font-mono text-[10px] text-text-muted tabular-nums">
					{filterCount(option.value)}
				</span>
			</button>
		{/each}
	</div>
</div>

{#if studio.catalogLoading && items.length === 0 && groups.length === 0}
	<div class="space-y-2">
		{#each [0, 1, 2, 3] as row (row)}
			<div class="h-[72px] animate-pulse rounded-xl border border-border bg-elevated"></div>
		{/each}
	</div>
{:else if studio.catalogError && items.length === 0 && groups.length === 0}
	<ErrorNotice error={studio.catalogError} onRetry={() => void studio.refreshCatalog()} />
{:else if visibleCount === 0}
	<EmptyState
		icon={Mic}
		title={query ? 'No voices match' : 'No voices in this filter'}
		description={query
			? `Nothing matches “${query}”. Clear the search or try another source.`
			: 'Clone a clip from the inbox, design a timbre, or show the engine presets.'}
	>
		<Button
			variant="outline"
			size="sm"
			onclick={() => {
				query = '';
				filter = 'all';
			}}
		>
			Clear filters
		</Button>
	</EmptyState>
{:else}
	<div
		class="hidden grid-cols-[minmax(0,1fr)_88px_86px_112px_auto] gap-3 px-3 pb-1 text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase lg:grid"
	>
		<span>Voice</span>
		<span>Duration</span>
		<span>Added</span>
		<span>Detail</span>
		<span class="text-right">Actions</span>
	</div>
	<div class="space-y-1.5">
		{#each filtered as item (item.id)}
			{#if item.kind === 'voice'}
				<VoiceCard
					{item}
					onUse={() => {
						studio.useVoice(item.voice);
						void goto(resolve('speak'));
					}}
					onDelete={() => remove(item.voice.id)}
				/>
			{/if}
		{/each}
		{#each filteredGroups as group (group.id)}
			<PresetGroupCard
				{group}
				onUse={(speaker: string) => {
					studio.usePreset({
						kind: 'preset',
						id: group.id,
						name: `${speaker} — ${group.engineName}`,
						engineId: group.engineId,
						engineName: group.engineName,
						modelAlias: group.modelAlias,
						speaker,
						supportsInstruct: group.supportsInstruct
					});
					void goto(resolve('speak'));
				}}
			/>
		{/each}
	</div>
{/if}

<DesignDrawer open={designOpen} onclose={() => (designOpen = false)} onSaved={saved} />
<CloneDrawer open={cloneOpen} onclose={() => (cloneOpen = false)} onSaved={saved} />
