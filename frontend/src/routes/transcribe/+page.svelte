<script lang="ts">
	import { onMount } from 'svelte';
	import Copy from '@lucide/svelte/icons/copy';
	import FileMusic from '@lucide/svelte/icons/file-music';
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import Upload from '@lucide/svelte/icons/upload';
	import { transcribeFile, describeApiError } from '#lib/api/studio.js';
	import type { ApiErrorPayload, TranscribeResult } from '#lib/api/types.js';
	import PageHeader from '#lib/components/page-header.svelte';
	import ErrorNotice from '#lib/components/studio/error-notice.svelte';
	import SelectField from '#lib/components/studio/select-field.svelte';
	import { Button } from '#lib/components/ui/button/index.js';
	import {
		downloadText,
		formatBytes,
		formatClock,
		formatRtf,
		formatTimestamp,
		shortText,
		toSrt,
		toVtt
	} from '#lib/format.js';
	import { asrEngines } from '#lib/speak.js';
	import { studio } from '#lib/stores/studio.svelte.js';

	let file = $state<File | null>(null);
	let input = $state<HTMLInputElement | null>(null);
	let dragging = $state(false);
	let engineId = $state('');
	let modelAlias = $state('');
	let language = $state('auto');
	let hotwords = $state('');
	let busy = $state(false);
	let error = $state<ApiErrorPayload | string>('');
	let result = $state<TranscribeResult | null>(null);
	let copied = $state(false);

	const installedAsr = $derived(asrEngines(studio.engines).filter((engine) => engine.installed));
	const engine = $derived(installedAsr.find((item) => item.id === engineId) ?? null);
	const engineOptions = $derived(
		installedAsr.map((item) => ({
			value: item.id,
			label: item.displayName.replace(/\s*\(.*$/, '')
		}))
	);
	const modelOptions = $derived(
		(engine?.models ?? []).map((model) => ({
			value: model.alias,
			label: `${model.alias}${model.downloaded ? '' : ' · not downloaded'}`,
			disabled: !model.downloaded
		}))
	);
	const languageOptions = $derived([
		{ value: 'auto', label: 'Auto-detect' },
		...(engine?.languages ?? []).map((item) => ({ value: item, label: item }))
	]);
	const modelValue = $derived(engineId && modelAlias ? `${engineId}:${modelAlias}` : '');

	$effect(() => {
		if (installedAsr.length === 0) return;
		if (!engineId) {
			const first = installedAsr[0];
			if (first) {
				engineId = first.id;
				modelAlias =
					first.models.find((model) => model.downloaded)?.alias ?? first.models[0]?.alias ?? '';
			}
		} else if (!modelAlias && engine) {
			modelAlias =
				engine.models.find((model) => model.downloaded)?.alias ?? engine.models[0]?.alias ?? '';
		}
	});

	onMount(() => {
		if (studio.engines.length === 0) void studio.refreshCatalog();
	});

	function chooseFile(next: File | null): void {
		file = next;
		result = null;
		error = '';
	}

	function onDrop(event: DragEvent): void {
		event.preventDefault();
		dragging = false;
		chooseFile(event.dataTransfer?.files[0] ?? null);
	}

	async function submit(): Promise<void> {
		if (!file) {
			error = 'Choose an audio or video file.';
			return;
		}
		if (!modelValue) {
			error = 'Choose an ASR engine and model.';
			return;
		}
		busy = true;
		error = '';
		result = null;
		try {
			result = await transcribeFile(file, {
				model: modelValue,
				language,
				hotwords
			});
		} catch (cause) {
			error = describeApiError(cause);
		} finally {
			busy = false;
		}
	}

	async function copyText(): Promise<void> {
		if (!result) return;
		try {
			await navigator.clipboard.writeText(result.text);
			copied = true;
			window.setTimeout(() => (copied = false), 1600);
		} catch {
			error = 'Could not copy to the clipboard.';
		}
	}

	function downloadSubtitles(kind: 'srt' | 'vtt'): void {
		if (!result) return;
		const name = shortText(file?.name ?? 'transcript', 40).replace(/\.[^.]+$/, '');
		if (kind === 'srt') {
			downloadText(`${name}.srt`, toSrt(result.segments), 'application/x-subrip');
		} else {
			downloadText(`${name}.vtt`, toVtt(result.segments), 'text/vtt');
		}
	}
</script>

<PageHeader
	title="Transcribe"
	description="Upload audio or video. Get timed text and subtitle files from the local daemon."
/>

<div class="grid items-start gap-4 xl:grid-cols-[340px_minmax(0,1fr)]">
	<div class="space-y-3">
		{#if error}
			<ErrorNotice {error} />
		{/if}

		<label
			for="transcribe-file"
			class={`flex min-h-[160px] cursor-pointer flex-col items-center justify-center rounded-xl border border-dashed px-4 py-6 text-center transition-colors ${
				dragging
					? 'border-primary/60 bg-primary/5'
					: 'border-border bg-elevated hover:bg-elevated-hover/50'
			}`}
			ondragover={(event) => {
				event.preventDefault();
				dragging = true;
			}}
			ondragleave={() => (dragging = false)}
			ondrop={onDrop}
		>
			<span
				class="mb-2 flex size-9 items-center justify-center rounded-lg bg-panel text-text-secondary"
			>
				<Upload class="size-4" aria-hidden="true" />
			</span>
			<span class="text-[12px] font-medium text-foreground">
				{file ? file.name : 'Drop a clip here'}
			</span>
			<span class="mt-1 text-[10px] text-text-muted">
				{file ? formatBytes(file.size) : 'or click to choose audio / video'}
			</span>
			<input
				id="transcribe-file"
				bind:this={input}
				class="sr-only"
				type="file"
				accept="audio/*,video/*"
				onchange={(event) => chooseFile(event.currentTarget.files?.[0] ?? null)}
			/>
		</label>

		<div class="rounded-xl border border-border bg-elevated p-3.5">
			<div class="space-y-3">
				<SelectField
					id="transcribe-engine"
					label="ASR engine"
					value={engineId}
					options={engineOptions}
					onchange={(value: string) => {
						engineId = value;
						const next = installedAsr.find((item) => item.id === value);
						modelAlias =
							next?.models.find((model) => model.downloaded)?.alias ?? next?.models[0]?.alias ?? '';
						language = 'auto';
						result = null;
					}}
					hint={installedAsr.length === 0
						? 'No installed ASR engine. Install Phonon or use mock_asr.'
						: undefined}
				/>
				<SelectField
					id="transcribe-model"
					label="Model"
					value={modelAlias}
					options={modelOptions}
					onchange={(value: string) => (modelAlias = value)}
				/>
				<SelectField
					id="transcribe-language"
					label="Language hint"
					value={language}
					options={languageOptions}
					onchange={(value: string) => (language = value)}
				/>
				<div>
					<label for="transcribe-hotwords" class="mb-1 block text-[11px] text-text-secondary">
						Hotwords <span class="normal-case">(comma separated, max 25)</span>
					</label>
					<input
						id="transcribe-hotwords"
						class="h-8 w-full rounded-lg border border-border bg-panel px-2.5 text-[12px] text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
						placeholder="CUDA, Manim, agent-voice"
						bind:value={hotwords}
					/>
				</div>
				<Button
					class="h-9 w-full"
					disabled={busy || !file || !modelValue}
					onclick={() => void submit()}
				>
					{#if busy}
						<LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
						Transcribing
					{:else}
						<FileMusic class="size-4" aria-hidden="true" />
						Transcribe
					{/if}
				</Button>
			</div>
		</div>
	</div>

	<div class="min-w-0">
		{#if !result}
			<div class="flex min-h-[320px] flex-col items-center justify-center px-6 text-center">
				<span
					class="mb-3 flex size-10 items-center justify-center rounded-lg bg-elevated text-text-muted"
				>
					<FileMusic class="size-5" aria-hidden="true" />
				</span>
				<p class="text-[13px] text-text-secondary">No transcript yet</p>
				<p class="mt-1 max-w-sm text-[11px] leading-5 text-text-muted">
					Drop a clip on the left. The transcript, timed segments and subtitle exports will appear
					here.
				</p>
			</div>
		{:else}
			<div class="space-y-3">
				<div
					class="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-border bg-elevated px-3 py-2 font-mono text-[10px] text-text-secondary tabular-nums"
				>
					<span>{result.engineId} · {result.modelAlias}</span>
					<span>{result.language || 'language unknown'}</span>
					<span>{formatClock(result.durationSec)}</span>
					<span>{formatRtf(result.realtimeMultiple)}</span>
					<span>{result.segmentsCount} segments</span>
					{#if result.truncated || result.segmentsOmitted > 0}
						<span class="text-warning">{result.segmentsOmitted} omitted in preview</span>
					{/if}
				</div>

				<section class="rounded-xl border border-border bg-elevated">
					<header class="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
						<h2 class="text-[11px] text-text-secondary">Transcript</h2>
						<div class="flex gap-1.5">
							<Button variant="ghost" size="xs" onclick={() => void copyText()}>
								<Copy class="size-3" aria-hidden="true" />
								{copied ? 'Copied' : 'Copy'}
							</Button>
						</div>
					</header>
					<div class="max-h-[280px] overflow-y-auto px-3.5 py-3">
						<p class="text-[13px] leading-6 whitespace-pre-wrap text-foreground">{result.text}</p>
					</div>
				</section>

				<section class="rounded-xl border border-border bg-elevated">
					<header
						class="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2"
					>
						<h2 class="text-[11px] text-text-secondary">Timed segments</h2>
						<div class="flex gap-1.5">
							<Button
								variant="outline"
								size="xs"
								disabled={result.segments.length === 0}
								onclick={() => downloadSubtitles('srt')}
							>
								Download .srt
							</Button>
							<Button
								variant="outline"
								size="xs"
								disabled={result.segments.length === 0}
								onclick={() => downloadSubtitles('vtt')}
							>
								Download .vtt
							</Button>
						</div>
					</header>
					<div class="max-h-[420px] overflow-y-auto">
						{#if result.segments.length === 0}
							<p class="px-3.5 py-4 text-[11px] text-text-muted">
								The backend returned no timed segments.
							</p>
						{:else}
							<div class="divide-y divide-border">
								{#each result.segments as segment (segment.index)}
									<div class="flex gap-3 px-3.5 py-2">
										<span
											class="w-[148px] shrink-0 font-mono text-[10px] whitespace-nowrap text-text-muted tabular-nums"
										>
											{formatTimestamp(segment.startSec)} – {formatTimestamp(segment.endSec)}
										</span>
										<p class="min-w-0 text-[12px] leading-5 text-text-secondary">{segment.text}</p>
									</div>
								{/each}
							</div>
						{/if}
					</div>
					{#if result.truncated || result.segmentsOmitted > 0}
						<p class="border-t border-border px-3.5 py-2 text-[10px] leading-4 text-warning">
							Inline segments are a bounded preview; {result.transcriptJsonPath
								? `the full JSON is at ${result.transcriptJsonPath}`
								: 'the daemon did not return a full JSON path'}.
						</p>
					{/if}
				</section>
			</div>
		{/if}
	</div>
</div>
