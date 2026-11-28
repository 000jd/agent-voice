<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import Plus from '@lucide/svelte/icons/plus';
	import Search from '@lucide/svelte/icons/search';
	import Upload from '@lucide/svelte/icons/upload';
	import { describeApiError } from '#lib/api/client.js';
	import { previewPreset } from '#lib/api/presets.js';
	import {
		deleteVoice,
		exportVoice,
		importVoice,
		patchVoice,
		previewVoice
	} from '#lib/api/voices.js';
	import type { ApiErrorPayload, PresetVoice, VoiceSummary } from '#lib/api/types.js';
	import { clipFromPreview } from '#lib/clips.js';
	import ConfirmDialog from '#lib/components/common/confirm-dialog.svelte';
	import Dialog from '#lib/components/common/dialog.svelte';
	import Dropzone from '#lib/components/common/dropzone.svelte';
	import EmptyState from '#lib/components/common/empty-state.svelte';
	import ErrorNotice from '#lib/components/common/error-notice.svelte';
	import Segmented from '#lib/components/common/segmented.svelte';
	import CloneDialog from '#lib/components/voices/clone-dialog.svelte';
	import DesignDialog from '#lib/components/voices/design-dialog.svelte';
	import PresetCard from '#lib/components/voices/preset-card.svelte';
	import VoiceCard from '#lib/components/voices/voice-card.svelte';
	import { downloadBlob, sanitizeFilename } from '#lib/format.js';
	import { clone } from '#lib/stores/clone.svelte.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';
	import { speak } from '#lib/stores/speak.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { toasts } from '#lib/stores/toasts.svelte.js';
	import {
		PREVIEW_TEXT,
		VOICE_FILTER_LABELS,
		filterPresets,
		filterVoices,
		presetAvailability,
		uniquePresets,
		voiceCompatibility,
		type VoiceFilter
	} from '#lib/voices.js';

	let filter = $state<VoiceFilter>('all');
	let query = $state('');
	let previewingId = $state<string | null>(null);
	let renameOpen = $state(false);
	let renameTarget = $state<VoiceSummary | null>(null);
	let renameValue = $state('');
	let renameBusy = $state(false);
	let renameError = $state<ApiErrorPayload | null>(null);
	let deleteOpen = $state(false);
	let deleteTarget = $state<VoiceSummary | null>(null);
	let designOpen = $state(false);
	let importing = $state(false);
	let importError = $state<ApiErrorPayload | null>(null);

	const voices = $derived(filterVoices(studio.voices, filter, query));
	const presets = $derived(filterPresets(studio.presets, filter, query));
	const allPresets = $derived(uniquePresets(studio.presets));
	const filters = $derived([
		{
			id: 'all',
			label: VOICE_FILTER_LABELS.all,
			count: studio.voices.length + allPresets.length
		},
		{
			id: 'cloned',
			label: VOICE_FILTER_LABELS.cloned,
			count: studio.voices.filter((voice) => voice.kind === 'cloned').length
		},
		{
			id: 'designed',
			label: VOICE_FILTER_LABELS.designed,
			count: studio.voices.filter((voice) => voice.kind === 'designed').length
		},
		{ id: 'preset', label: VOICE_FILTER_LABELS.preset, count: allPresets.length }
	]);
	const showVoicesSection = $derived(filter !== 'preset');
	const showPresetSection = $derived(filter === 'all' || filter === 'preset');
	const empty = $derived(!studio.catalogLoading && studio.voices.length === 0);

	onMount(() => {
		if (studio.engines.length === 0) void studio.refreshCatalog();
	});

	function useVoice(voice: VoiceSummary): void {
		speak.selectVoice(voice);
		void goto(resolve('speak'));
	}

	function usePreset(preset: PresetVoice): void {
		speak.selectPreset(preset);
		void goto(resolve('speak'));
	}

	async function previewVoiceCard(voice: VoiceSummary): Promise<void> {
		previewingId = voice.id;
		try {
			const preview = await previewVoice(voice.id, PREVIEW_TEXT);
			void getPlayer().play(clipFromPreview(preview, voice.name));
		} catch (error) {
			toasts.error('Preview failed', describeApiError(error).message);
		} finally {
			previewingId = null;
		}
	}

	async function previewPresetCard(preset: PresetVoice): Promise<void> {
		previewingId = preset.presetId;
		try {
			const preview = await previewPreset({
				engine: preset.engineId,
				model: preset.modelAlias,
				speaker: preset.speaker,
				text: PREVIEW_TEXT
			});
			void getPlayer().play(clipFromPreview(preview, preset.displayName));
		} catch (error) {
			toasts.error('Preview failed', describeApiError(error).message);
		} finally {
			previewingId = null;
		}
	}

	function openRename(voice: VoiceSummary): void {
		renameTarget = voice;
		renameValue = voice.name;
		renameError = null;
		renameOpen = true;
	}

	async function submitRename(): Promise<void> {
		if (!renameTarget) return;
		renameBusy = true;
		renameError = null;
		try {
			await patchVoice(renameTarget.id, { name: renameValue.trim() });
			await studio.refreshVoices();
			renameOpen = false;
			renameTarget = null;
		} catch (error) {
			renameError = describeApiError(error);
		} finally {
			renameBusy = false;
		}
	}

	async function exportCard(voice: VoiceSummary): Promise<void> {
		try {
			const blob = await exportVoice(voice.id);
			downloadBlob(`${sanitizeFilename(voice.name)}.voice.zip`, blob);
		} catch (error) {
			toasts.error('Export failed', describeApiError(error).message);
		}
	}

	function askDelete(voice: VoiceSummary): void {
		deleteTarget = voice;
		deleteOpen = true;
	}

	async function confirmDelete(): Promise<void> {
		const voice = deleteTarget;
		if (!voice) return;
		try {
			await deleteVoice(voice.id);
			await studio.refreshVoices();
			if (speak.config.voiceId === voice.id) speak.syncCatalog();
			toasts.success('Voice deleted', voice.name);
		} catch (error) {
			toasts.error('Delete failed', describeApiError(error).message);
		} finally {
			deleteOpen = false;
			deleteTarget = null;
		}
	}

	async function onImport(file: File): Promise<void> {
		importing = true;
		importError = null;
		try {
			const created = await importVoice(file);
			await studio.refreshVoices();
			toasts.success('Voice imported', created.name);
		} catch (error) {
			importError = describeApiError(error);
		} finally {
			importing = false;
		}
	}

	function startCloneWithFile(file: File): void {
		clone.begin('create');
		void clone.acceptFile(file);
	}
</script>

<div class="h-full overflow-y-auto">
	<div class="mx-auto w-full max-w-[1400px] px-4 py-4 md:px-6 md:py-5">
		<div class="flex flex-wrap items-center gap-2">
			<div class="relative min-w-[180px] flex-1">
				<Search
					class="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-muted"
					aria-hidden="true"
				/>
				<label class="sr-only" for="voice-search">Search voices</label>
				<input
					id="voice-search"
					class="input h-8 pl-8 text-[12px]"
					type="search"
					placeholder="Search voices and presets"
					bind:value={query}
				/>
			</div>
			<Segmented
				items={filters}
				value={filter}
				label="Filter voices"
				onchange={(id) => (filter = id as VoiceFilter)}
			/>
			<div class="ml-auto flex items-center gap-2">
				<label
					class={`btn btn-sm cursor-pointer ${importing ? 'pointer-events-none opacity-50' : ''}`}
				>
					<Upload class="size-3.5" aria-hidden="true" />
					{importing ? 'Importing…' : 'Import'}
					<input
						class="sr-only"
						type="file"
						accept=".zip,application/zip"
						onchange={(event) => {
							const file = event.currentTarget.files?.[0];
							if (file) void onImport(file);
							event.currentTarget.value = '';
						}}
					/>
				</label>
				<button type="button" class="btn btn-sm" onclick={() => (designOpen = true)}>Design</button>
				<button type="button" class="btn btn-sm btn-primary" onclick={() => clone.begin('create')}>
					<Plus class="size-3.5" aria-hidden="true" />
					Clone voice
				</button>
			</div>
		</div>

		{#if importError}
			<div class="mt-3">
				<ErrorNotice
					error={importError}
					onRetry={() => (importError = null)}
					retryLabel="Dismiss"
				/>
			</div>
		{/if}

		{#if studio.catalogLoading && studio.voices.length === 0}
			<div class="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
				{#each Array.from({ length: 6 }, (_, index) => index) as index (index)}
					<div class="card h-[176px] p-3">
						<div class="skeleton size-9"></div>
						<div class="skeleton mt-3 h-3 w-2/3"></div>
						<div class="skeleton mt-2 h-3 w-1/2"></div>
						<div class="skeleton mt-6 h-7 w-full"></div>
					</div>
				{/each}
			</div>
		{:else}
			{#if studio.catalogError && studio.voices.length === 0}
				<div class="mt-4">
					<ErrorNotice error={studio.catalogError} onRetry={() => void studio.refreshCatalog()} />
				</div>
			{/if}

			{#if showVoicesSection}
				<section class="mt-5" aria-labelledby="your-voices">
					<div class="mb-2 flex items-baseline justify-between gap-3">
						<h2 id="your-voices" class="text-[13px] font-medium text-ink">
							Your voices
							<span class="metric ml-1 text-[11px] text-ink-muted">{studio.voices.length}</span>
						</h2>
						{#if filter !== 'all'}
							<button
								type="button"
								class="text-[11px] text-ink-secondary hover:text-ink"
								onclick={() => (filter = 'all')}
							>
								Show all
							</button>
						{/if}
					</div>

					{#if voices.length === 0}
						{#if empty}
							<div class="rounded-panel border border-rule bg-surface p-4">
								<div class="grid gap-4 md:grid-cols-[minmax(0,1fr)_280px]">
									<div class="min-w-0">
										<h3 class="text-[15px] font-semibold text-ink">Clone your first voice</h3>
										<p class="mt-1 max-w-[52ch] text-[12px] leading-5 text-ink-secondary">
											Drop a clean recording of a voice you have the right to use. The daemon trims
											it, measures it and stores it locally; nothing leaves this machine.
										</p>
										<ol class="metric mt-3 space-y-1 text-[11px] text-ink-muted">
											<li>1 · Six to twenty seconds of one speaker</li>
											<li>2 · No music, no noise, no clipping</li>
											<li>3 · An accurate transcript improves the clone</li>
										</ol>
									</div>
									<Dropzone
										id="voices-hero-file"
										label="Drop a clip or choose a file"
										hint="WAV, FLAC, Ogg, MP3, M4A"
										onfile={startCloneWithFile}
									/>
								</div>
							</div>
						{:else}
							<EmptyState
								title="No voices match this filter"
								description="Clear the search or pick another filter to see your voices again."
							>
								<button
									type="button"
									class="btn btn-sm"
									onclick={() => {
										query = '';
										filter = 'all';
									}}
								>
									Clear filters
								</button>
							</EmptyState>
						{/if}
					{:else}
						<ul class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
							{#each voices as voice (voice.id)}
								<li>
									<VoiceCard
										{voice}
										compatible={voiceCompatibility(voice, studio.engines) !== null}
										previewing={previewingId === voice.id}
										onpreview={() => void previewVoiceCard(voice)}
										onuse={() => useVoice(voice)}
										onrename={() => openRename(voice)}
										onsample={() => clone.begin('add-sample', voice)}
										onexport={() => void exportCard(voice)}
										ondelete={() => askDelete(voice)}
									/>
								</li>
							{/each}
						</ul>
					{/if}
				</section>
			{/if}

			{#if showPresetSection}
				<section class="mt-6" aria-labelledby="built-in-presets">
					<div class="mb-2 flex items-baseline justify-between gap-3">
						<h2 id="built-in-presets" class="text-[13px] font-medium text-ink">
							Built-in presets
							<span class="metric ml-1 text-[11px] text-ink-muted">{allPresets.length}</span>
						</h2>
						<p class="text-[11px] text-ink-muted">
							One card per speaker, from the engine manifests.
						</p>
					</div>
					{#if presets.length === 0}
						<div class="rounded-panel border border-rule bg-surface px-4 py-6">
							<p class="max-w-[70ch] text-[12px] leading-5 text-ink-secondary">
								No preset speakers are declared by the installed engines. Presets appear when a
								manifest lists <code class="metric">preset_speakers</code> for a custom-voice model.
							</p>
						</div>
					{:else}
						<ul class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
							{#each presets as preset (preset.presetId)}
								<li>
									<PresetCard
										{preset}
										availability={presetAvailability(studio.engines, preset)}
										previewing={previewingId === preset.presetId}
										onpreview={() => void previewPresetCard(preset)}
										onuse={() => usePreset(preset)}
									/>
								</li>
							{/each}
						</ul>
					{/if}
				</section>
			{/if}
		{/if}
	</div>
</div>

<CloneDialog />
<DesignDialog bind:open={designOpen} />

<Dialog
	bind:open={renameOpen}
	title="Rename voice"
	size="sm"
	onclose={() => {
		renameTarget = null;
		renameError = null;
	}}
>
	<div class="space-y-3">
		<div class="space-y-1">
			<label class="field-label" for="rename-voice">Name</label>
			<input id="rename-voice" class="input" type="text" maxlength="64" bind:value={renameValue} />
		</div>
		{#if renameError}
			<ErrorNotice error={renameError} compact />
		{/if}
	</div>
	{#snippet footer()}
		<button type="button" class="btn" onclick={() => (renameOpen = false)}>Cancel</button>
		<button
			type="button"
			class="btn btn-primary"
			disabled={renameBusy || renameValue.trim().length === 0}
			onclick={() => void submitRename()}
		>
			{renameBusy ? 'Saving…' : 'Save name'}
		</button>
	{/snippet}
</Dialog>

<ConfirmDialog
	bind:open={deleteOpen}
	title="Delete voice"
	message={`Delete “${deleteTarget?.name ?? ''}”?`}
	detail="The reference audio, samples and lexicon for this voice are removed from disk. Existing history entries keep their text but lose the audio."
	confirmLabel="Delete voice"
	danger
	onconfirm={() => void confirmDelete()}
	oncancel={() => (deleteTarget = null)}
/>
