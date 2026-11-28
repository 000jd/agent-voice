<script lang="ts">
	import Play from '@lucide/svelte/icons/play';
	import Monogram from '#lib/components/common/monogram.svelte';
	import type { PresetVoice } from '#lib/api/types.js';

	let {
		preset,
		availability = 'ready',
		previewing = false,
		onpreview,
		onuse
	}: {
		preset: PresetVoice;
		availability?: 'ready' | 'needs-download' | 'unavailable';
		previewing?: boolean;
		onpreview: () => void;
		onuse: () => void;
	} = $props();

	const statusLabel = $derived(
		availability === 'ready'
			? 'Ready'
			: availability === 'needs-download'
				? 'Needs download'
				: 'Engine unavailable'
	);
</script>

<article class="card flex flex-col p-3" data-testid={`preset-card-${preset.presetId}`}>
	<div class="flex items-start gap-2.5">
		<Monogram name={preset.displayName} kind="preset" />
		<div class="min-w-0 flex-1">
			<h3 class="truncate text-[13px] leading-5 font-medium text-ink">{preset.displayName}</h3>
			<p class="truncate text-[11px] text-ink-secondary">Preset · {preset.speaker}</p>
		</div>
		<span
			class={`chip shrink-0 ${availability === 'ready' ? 'chip-success' : availability === 'needs-download' ? 'chip-signal' : ''}`}
		>
			{statusLabel}
		</span>
	</div>

	<p class="metric mt-2 truncate text-[11px] text-ink-muted">
		{preset.engineId} · {preset.modelAlias}
	</p>
	<p class="mt-0.5 text-[11px] text-ink-secondary">
		{preset.languages.length > 0 ? preset.languages.join(', ') : 'Language not declared'}
	</p>

	<div class="mt-auto flex items-center gap-1.5 pt-3">
		<button
			type="button"
			class="btn btn-sm flex-1"
			disabled={previewing || availability === 'unavailable'}
			onclick={onpreview}
		>
			<Play class="size-3" aria-hidden="true" />
			{previewing ? 'Rendering…' : 'Preview'}
		</button>
		<button
			type="button"
			class="btn btn-sm btn-primary flex-1"
			disabled={availability === 'unavailable'}
			onclick={onuse}
		>
			Use in Speak
		</button>
	</div>
</article>
