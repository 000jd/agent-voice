<script lang="ts">
	import Boxes from '@lucide/svelte/icons/boxes';
	import type { EngineSummary } from '#lib/api/types.js';
	import StatusBadge from './status-badge.svelte';
	import { formatVram } from '#lib/format.js';
	import { MODE_LABELS } from '#lib/speak.js';

	let { engine }: { engine: EngineSummary } = $props();

	const device = $derived(engine.resolvedDevice ?? engine.defaultDevice ?? engine.device);
</script>

<article class="min-w-0 overflow-hidden rounded-xl border border-border bg-elevated">
	<header class="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3">
		<div class="flex min-w-0 items-start gap-2.5">
			<span
				class="flex size-8 shrink-0 items-center justify-center rounded-lg bg-panel text-text-secondary"
			>
				<Boxes class="size-4" aria-hidden="true" />
			</span>
			<div class="min-w-0">
				<h2 class="truncate text-[13px] font-medium text-foreground">{engine.displayName}</h2>
				<p class="truncate font-mono text-[10px] text-text-muted">
					{engine.id} · {engine.backend}
					{engine.backendVersion}
				</p>
			</div>
		</div>
		<div class="flex flex-wrap items-center gap-1.5">
			<StatusBadge
				tone={engine.installed ? 'success' : 'warning'}
				label={engine.installed ? 'Installed' : 'Not installed'}
			/>
			<StatusBadge
				tone={engine.kind === 'tts' ? 'accent' : 'neutral'}
				label={engine.kind.toUpperCase()}
			/>
			<StatusBadge tone="neutral" label={device.toUpperCase()} />
		</div>
	</header>

	<div class="space-y-3 px-4 py-3">
		<div class="flex min-w-0 flex-wrap gap-x-4 gap-y-1 text-[10px] break-words text-text-muted">
			<span>Devices: {engine.supportedDevices.join(' · ') || '—'}</span>
			{#if engine.languages.length > 0}
				<span
					>Languages: {engine.languages.slice(0, 6).join(' · ')}{engine.languages.length > 6
						? ` +${engine.languages.length - 6}`
						: ''}</span
				>
			{/if}
		</div>

		{#if engine.whyUnavailable}
			<p
				class="rounded-md border border-warning/20 bg-warning/10 px-2 py-1.5 text-[10px] leading-4 text-warning"
			>
				{engine.whyUnavailable}
			</p>
		{/if}

		{#if engine.models.length === 0}
			<p class="text-[11px] text-text-muted">No checkpoints declared.</p>
		{:else}
			<div class="overflow-hidden rounded-lg border border-border">
				{#each engine.models as model, index (model.alias)}
					<div
						class:border-t={index > 0}
						class="grid gap-2 border-border px-2.5 py-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center"
					>
						<div class="min-w-0">
							<div class="flex flex-wrap items-center gap-1.5">
								<span class="truncate font-mono text-[11px] text-foreground">{model.alias}</span>
								<StatusBadge tone="neutral" label={MODE_LABELS[model.mode]} />
								<StatusBadge tone="neutral" label={model.qualityTier} />
								{#if model.loaded}
									<StatusBadge tone="success" label="Loaded" />
								{:else if model.downloaded}
									<StatusBadge tone="neutral" label="Downloaded" />
								{:else}
									<StatusBadge tone="warning" label="Not local" />
								{/if}
							</div>
							<p class="mt-0.5 text-[10px] text-text-muted">
								<span class="font-mono tabular-nums">{formatVram(model.estVramGb * 1024)}</span>
								{#if model.presetSpeakers.length}
									<span> · {model.presetSpeakers.length} preset speakers</span>
								{/if}
								{#if model.supportsInstruct}
									<span> · instruct</span>
								{/if}
							</p>
						</div>
						<span class="hidden text-right font-mono text-[10px] text-text-muted sm:block">
							{model.isDefault ? 'default' : ''}
						</span>
					</div>
				{/each}
			</div>
		{/if}
	</div>
</article>
