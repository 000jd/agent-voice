<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import Copy from '@lucide/svelte/icons/copy';
	import Download from '@lucide/svelte/icons/download';
	import Segmented from '#lib/components/common/segmented.svelte';
	import CloneDialog from '#lib/components/voices/clone-dialog.svelte';
	import CaptureList from '#lib/components/transcribe/capture-list.svelte';
	import TranscriptEditor from '#lib/components/transcribe/transcript-editor.svelte';
	import { formatClock, formatDateTime } from '#lib/format.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { transcribe } from '#lib/stores/transcribe.svelte.js';
	import type { CaptureExportFormat } from '#lib/api/captures.js';

	let view = $state<'captures' | 'transcript' | 'export'>('transcript');

	const views = [
		{ id: 'captures', label: 'Captures' },
		{ id: 'transcript', label: 'Transcript' },
		{ id: 'export', label: 'Export' }
	];

	const exportFormats: { id: CaptureExportFormat; label: string }[] = [
		{ id: 'srt', label: 'SRT' },
		{ id: 'vtt', label: 'VTT' },
		{ id: 'txt', label: 'TXT' },
		{ id: 'json', label: 'JSON' }
	];

	const languageOptions = $derived(
		transcribe.selectedEngine && transcribe.selectedEngine.languages.length > 0
			? ['auto', ...transcribe.selectedEngine.languages.filter((item) => item !== 'Auto')]
			: ['auto']
	);

	onMount(() => {
		if (studio.engines.length === 0) void studio.refreshCatalog();
		void transcribe.init(page.url.searchParams.get('capture'));
	});

	$effect(() => {
		if (studio.asrEngines.length === 0) return;
		transcribe.pickEngine();
	});
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="shrink-0 border-b border-rule bg-panel px-3 py-1.5 lg:hidden">
		<Segmented
			items={views}
			value={view}
			label="Transcribe pane"
			onchange={(id) => (view = id as typeof view)}
		/>
	</div>

	<div class="flex min-h-0 flex-1">
		<aside
			class={`${view === 'captures' ? 'flex' : 'hidden'} min-h-0 w-full shrink-0 flex-col border-rule bg-panel lg:flex lg:w-[300px] lg:border-r`}
			aria-label="Captures"
		>
			<CaptureList />
		</aside>

		<section
			class={`${view === 'transcript' ? 'flex' : 'hidden'} min-h-0 min-w-0 flex-1 flex-col lg:flex`}
			aria-label="Transcript"
		>
			<TranscriptEditor />
		</section>

		<aside
			class={`${view === 'export' ? 'flex' : 'hidden'} min-h-0 w-full shrink-0 flex-col overflow-y-auto border-rule bg-panel lg:flex lg:w-[290px] lg:border-l`}
			aria-label="Transcription settings and export"
		>
			<div class="space-y-3 p-3">
				<section class="space-y-2" aria-labelledby="asr-settings">
					<h2 id="asr-settings" class="text-[12px] font-medium text-ink-secondary">
						Transcription settings
					</h2>
					{#if studio.asrEngines.length === 0}
						<p class="text-[12px] leading-5 text-ink-secondary">
							No ASR engine is installed. Install one on Models; the daemon ships
							<code class="metric">mock_asr</code> for a dependency-free check.
						</p>
					{:else}
						<div class="space-y-1">
							<label class="field-label" for="asr-engine">Engine</label>
							<select
								id="asr-engine"
								class="select h-8 text-[12px]"
								value={transcribe.engineId}
								onchange={(event) => void transcribe.selectEngine(event.currentTarget.value)}
							>
								{#each studio.asrEngines as engine (engine.id)}
									<option value={engine.id}>
										{engine.displayName}{engine.installed ? '' : ' (not installed)'}
									</option>
								{/each}
							</select>
						</div>
						<div class="space-y-1">
							<label class="field-label" for="asr-model">Model</label>
							<select
								id="asr-model"
								class="select h-8 text-[12px]"
								value={transcribe.modelAlias}
								onchange={(event) => (transcribe.modelAlias = event.currentTarget.value)}
							>
								{#each transcribe.models as model (model.alias)}
									<option value={model.alias}>
										{model.alias}{model.downloaded ? '' : ' · not downloaded'}
									</option>
								{/each}
							</select>
							{#if transcribe.modelsLoading}
								<p class="text-[11px] text-ink-muted">Reading models…</p>
							{/if}
						</div>
						<div class="space-y-1">
							<label class="field-label" for="asr-language">Language</label>
							<select
								id="asr-language"
								class="select h-8 text-[12px]"
								bind:value={transcribe.language}
							>
								{#each languageOptions as language (language)}
									<option value={language}>{language === 'auto' ? 'Auto-detect' : language}</option>
								{/each}
							</select>
						</div>
						<div class="space-y-1">
							<label class="field-label" for="asr-hotwords">Hotwords</label>
							<input
								id="asr-hotwords"
								class="input h-8 text-[12px]"
								type="text"
								placeholder="agent-voice, Qwen, Kokoro"
								bind:value={transcribe.hotwords}
							/>
							<p class="text-[11px] leading-4 text-ink-muted">
								Comma-separated terms bias the decoder. Applied to the next upload.
							</p>
						</div>
					{/if}
				</section>

				{#if transcribe.detail}
					<section class="border-t border-rule pt-3" aria-labelledby="capture-meta">
						<h2 id="capture-meta" class="mb-1.5 text-[12px] font-medium text-ink-secondary">
							Selected capture
						</h2>
						<dl class="metric space-y-1 text-[11px]">
							<div class="flex justify-between gap-2">
								<dt class="text-ink-muted">Duration</dt>
								<dd class="text-ink">{formatClock(transcribe.detail.durationSec)}</dd>
							</div>
							<div class="flex justify-between gap-2">
								<dt class="text-ink-muted">Segments</dt>
								<dd class="text-ink">{transcribe.detail.segmentsCount}</dd>
							</div>
							<div class="flex justify-between gap-2">
								<dt class="text-ink-muted">Language</dt>
								<dd class="text-ink">{transcribe.detail.language || 'not reported'}</dd>
							</div>
							<div class="flex justify-between gap-2">
								<dt class="text-ink-muted">Created</dt>
								<dd class="text-ink">{formatDateTime(transcribe.detail.createdAt)}</dd>
							</div>
							<div class="flex justify-between gap-2">
								<dt class="text-ink-muted">Source</dt>
								<dd class="text-ink">{transcribe.detail.source}</dd>
							</div>
							<div class="flex justify-between gap-2">
								<dt class="text-ink-muted">ID</dt>
								<dd class="truncate text-ink" title={transcribe.detail.id}>
									{transcribe.detail.id}
								</dd>
							</div>
						</dl>
					</section>

					<section class="border-t border-rule pt-3" aria-labelledby="capture-export">
						<h2 id="capture-export" class="mb-1.5 text-[12px] font-medium text-ink-secondary">
							Export
						</h2>
						<div class="grid grid-cols-2 gap-1.5">
							{#each exportFormats as formatOpt (formatOpt.id)}
								<button
									type="button"
									class="btn btn-sm justify-start gap-1.5"
									disabled={transcribe.exporting !== null}
									onclick={() => void transcribe.exportAs(formatOpt.id)}
								>
									<Download class="size-3" aria-hidden="true" />
									{transcribe.exporting === formatOpt.id ? '…' : formatOpt.label}
								</button>
							{/each}
						</div>
						<button
							type="button"
							class="btn btn-sm btn-ghost mt-1.5 w-full justify-start gap-1.5"
							onclick={() => void transcribe.copyText('srt')}
						>
							<Copy class="size-3" aria-hidden="true" />
							Copy SRT to clipboard
						</button>
						<p class="mt-1 text-[11px] leading-4 text-ink-muted">
							Subtitles are rendered by the daemon from its stored segments, not re-derived in the
							browser.
						</p>
					</section>

					<section class="border-t border-rule pt-3">
						<button
							type="button"
							class="btn btn-sm w-full"
							onclick={() => void transcribe.useAsSample()}
						>
							Use as a voice sample
						</button>
						<p class="mt-1 text-[11px] leading-4 text-ink-muted">
							Creates a voice draft from this capture, then opens the clone review so you can trim
							it, check the transcript and save a voice.
						</p>
					</section>
				{:else}
					<p class="border-t border-rule pt-3 text-[12px] leading-5 text-ink-secondary">
						Select a capture to see its metadata and export it.
					</p>
				{/if}
			</div>
		</aside>
	</div>
</div>

<CloneDialog />
