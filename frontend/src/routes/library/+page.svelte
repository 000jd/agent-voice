<script lang="ts">
	import { onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import LibraryIcon from '@lucide/svelte/icons/library';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import EmptyState from '#lib/components/common/empty-state.svelte';
	import ErrorNotice from '#lib/components/common/error-notice.svelte';
	import Segmented from '#lib/components/common/segmented.svelte';
	import CaptureRow from '#lib/components/library/capture-row.svelte';
	import HistoryRow from '#lib/components/library/history-row.svelte';
	import { LIBRARY_FILTERS, library, type LibraryFilter } from '#lib/stores/library.svelte.js';

	const filters = LIBRARY_FILTERS.map((filter) => ({ id: filter.id, label: filter.label }));

	onMount(() => {
		void library.refresh();
	});
</script>

{#snippet libraryIcon()}
	<LibraryIcon class="size-5" aria-hidden="true" />
{/snippet}

<div class="h-full overflow-y-auto">
	<div class="mx-auto w-full max-w-[1200px] px-4 py-4 md:px-6">
		<div class="flex flex-wrap items-center gap-2">
			<Segmented
				items={filters}
				value={library.filter}
				label="Filter library"
				onchange={(id) => library.setFilter(id as LibraryFilter)}
			/>
			<div class="relative min-w-[180px] flex-1">
				<label class="sr-only" for="library-search">Search the library</label>
				<input
					id="library-search"
					class="input h-8 text-[12px]"
					type="search"
					placeholder="Search transcripts, scripts and designs"
					value={library.query}
					onchange={(event) => void library.search(event.currentTarget.value)}
				/>
			</div>
			<button
				type="button"
				class="btn btn-sm btn-ghost btn-icon"
				aria-label="Refresh library"
				title="Refresh library"
				onclick={() => void library.refresh()}
			>
				<RefreshCw class="size-3.5" aria-hidden="true" />
			</button>
		</div>

		{#if library.error}
			<div class="mt-3">
				<ErrorNotice error={library.error} onRetry={() => void library.refresh()} />
			</div>
		{/if}

		<div class="mt-4 rounded-panel border border-rule bg-surface">
			{#if library.loading && library.rows.length === 0}
				<div class="space-y-2 p-3">
					{#each Array.from({ length: 6 }, (_, index) => index) as index (index)}
						<div class="skeleton h-10 w-full"></div>
					{/each}
				</div>
			{:else if library.rows.length === 0}
				<div class="p-3">
					<EmptyState
						title={library.filter === 'favorite'
							? 'No favourites yet'
							: library.filter === 'transcribe'
								? 'No transcriptions yet'
								: 'The library is empty'}
						description={library.filter === 'favorite'
							? 'Star a take in Speak, or a transcription here, and it stays in this filter.'
							: library.filter === 'transcribe'
								? 'Upload or record audio on Transcribe; every capture is kept here until the retention limit removes it.'
								: 'Every render and transcription is stored on disk by the daemon, so it survives a restart. Generate a line or transcribe a clip to start.'}
						icon={libraryIcon}
					>
						<a class="btn btn-sm btn-primary" href={resolve('speak')}>Open Speak</a>
						<a class="btn btn-sm" href={resolve('transcribe')}>Open Transcribe</a>
					</EmptyState>
				</div>
			{:else}
				<ul class="divide-y divide-rule">
					{#each library.rows as row (row.id)}
						{#if row.kind === 'history'}
							<HistoryRow item={row.item} />
						{:else}
							<CaptureRow
								capture={row.capture}
								href={`${resolve('transcribe')}?capture=${row.capture.id}`}
							/>
						{/if}
					{/each}
				</ul>
				{#if library.hasMore}
					<div class="border-t border-rule p-2">
						<button type="button" class="btn btn-sm w-full" onclick={() => void library.loadMore()}>
							Load more
						</button>
					</div>
				{/if}
			{/if}
		</div>

		<p class="mt-3 text-[11px] text-ink-muted">
			Stored by the daemon under its data directory. Deleting an entry here removes it from disk;
			the retention limits on Settings do the rest.
		</p>
	</div>
</div>
