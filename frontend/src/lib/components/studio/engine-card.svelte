<script lang="ts">
	import Boxes from '@lucide/svelte/icons/boxes';
	import type { EngineSummary } from '#lib/api/types.js';
	import { formatVram } from '#lib/format.js';
	import { MODE_LABELS } from '#lib/speak.js';

	let { engine }: { engine: EngineSummary } = $props();

	const device = $derived(engine.resolvedDevice ?? engine.defaultDevice ?? engine.device);
</script>

<article class="min-w-0 overflow-hidden px-4 py-4 transition-colors hover:bg-elevated-hover/25">
	<header class="flex flex-wrap items-start justify-between gap-3">
		<div class="flex min-w-0 items-start gap-3">
			<span
				class="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg bg-elevated text-text-muted"
			>
				<Boxes class="size-4" aria-hidden="true" />
			</span>
			<div class="min-w-0">
				<h2 class="truncate text-[14px] font-medium text-foreground">{engine.displayName}</h2>
				<p class="mt-0.5 truncate font-mono text-[11px] text-text-muted">
					{engine.id} · {engine.backend}
					{engine.backendVersion}
				</p>
			</div>
		</div>
		<div class="flex items-center gap-3 text-[11px]">
			<span class={engine.installed ? 'text-success' : 'text-warning'}>
				{engine.installed ? 'Installed' : 'Not installed'}
			</span>
			<span class="font-mono text-[10px] text-text-secondary">{device.toUpperCase()}</span>
		</div>
	</header>

	<div
		class="mt-2 flex min-w-0 flex-wrap gap-x-4 gap-y-1 pl-11 text-[10px] break-words text-text-muted"
	>
		<span>{engine.kind.toUpperCase()}</span>
		<span>Devices: {engine.supportedDevices.join(' · ') || '—'}</span>
		{#if engine.languages.length > 0}
			<span>
				Languages: {engine.languages.slice(0, 7).join(' · ')}{engine.languages.length > 7
					? ` +${engine.languages.length - 7}`
					: ''}
			</span>
		{/if}
	</div>

	{#if engine.whyUnavailable}
		<p class="mt-2 pl-11 text-[10px] leading-4 text-warning">{engine.whyUnavailable}</p>
	{/if}

	{#if engine.models.length === 0}
		<p class="mt-3 pl-11 text-[11px] text-text-muted">No checkpoints declared.</p>
	{:else}
		<div class="mt-3 divide-y divide-border border-t border-border pl-11">
			{#each engine.models as model (model.alias)}
				<div class="grid gap-1.5 py-2.5 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
					<div class="min-w-0">
						<div class="flex flex-wrap items-center gap-x-2 gap-y-0.5">
							<span class="font-mono text-[12px] text-foreground">{model.alias}</span>
							<span class="text-[11px] text-text-muted"
								>{engine.kind === 'tts' ? MODE_LABELS[model.mode] : 'Transcription'}</span
							>
							<span class="text-[11px] text-text-muted">{model.qualityTier}</span>
							{#if model.supportsInstruct}
								<span class="text-[11px] text-text-muted">instruct</span>
							{/if}
						</div>
						<p class="mt-0.5 text-[11px] text-text-muted">
							<span class="font-mono tabular-nums">{formatVram(model.estVramGb * 1024)}</span>
							{#if model.presetSpeakers.length}
								<span> · {model.presetSpeakers.length} preset speakers</span>
							{/if}
						</p>
					</div>
					<div class="flex items-center justify-between gap-2 sm:justify-end">
						<span class="text-[11px] text-text-muted">{model.isDefault ? 'default' : ''}</span>
						<span
							class={model.loaded
								? 'text-success'
								: model.downloaded
									? 'text-text-secondary'
									: 'text-warning'}
						>
							{model.loaded ? 'Loaded' : model.downloaded ? 'Local' : 'Not local'}
						</span>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</article>
