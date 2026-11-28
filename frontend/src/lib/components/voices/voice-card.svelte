<script lang="ts">
	import Ellipsis from '@lucide/svelte/icons/ellipsis';
	import Play from '@lucide/svelte/icons/play';
	import Monogram from '#lib/components/common/monogram.svelte';
	import type { VoiceSummary } from '#lib/api/types.js';
	import { formatClock } from '#lib/format.js';
	import { voiceKindLabel, voiceLanguageLabel } from '#lib/voices.js';

	let {
		voice,
		previewing = false,
		compatible = true,
		onpreview,
		onuse,
		onrename,
		onsample,
		onexport,
		ondelete
	}: {
		voice: VoiceSummary;
		previewing?: boolean;
		compatible?: boolean;
		onpreview: () => void;
		onuse: () => void;
		onrename: () => void;
		onsample: () => void;
		onexport: () => void;
		ondelete: () => void;
	} = $props();

	let menu = $state<HTMLDetailsElement | null>(null);

	function run(action: () => void): void {
		if (menu) menu.open = false;
		action();
	}
</script>

<article class="card flex flex-col p-3" data-testid={`voice-card-${voice.id}`}>
	<div class="flex items-start gap-2.5">
		<Monogram name={voice.name} />
		<div class="min-w-0 flex-1">
			<h3 class="truncate text-[13px] leading-5 font-medium text-ink">{voice.name}</h3>
			<p class="truncate text-[11px] text-ink-secondary">
				{voiceKindLabel(voice)} · {voiceLanguageLabel(voice)}
			</p>
		</div>
		<details class="relative shrink-0" bind:this={menu}>
			<summary
				class="btn btn-ghost btn-icon cursor-pointer list-none [&::-webkit-details-marker]:hidden"
				aria-label={`More actions for ${voice.name}`}
				title="More actions"
			>
				<Ellipsis class="size-4" aria-hidden="true" />
			</summary>
			<div
				class="absolute right-0 z-20 mt-1 flex w-48 flex-col rounded-control border border-rule-strong bg-surface p-1"
				role="menu"
				aria-label={`Actions for ${voice.name}`}
			>
				<button
					type="button"
					class="btn btn-ghost btn-sm justify-start"
					role="menuitem"
					onclick={() => run(onrename)}
				>
					Rename
				</button>
				<button
					type="button"
					class="btn btn-ghost btn-sm justify-start"
					role="menuitem"
					onclick={() => run(onsample)}
				>
					Add sample
				</button>
				<button
					type="button"
					class="btn btn-ghost btn-sm justify-start"
					role="menuitem"
					onclick={() => run(onexport)}
				>
					Export .voice.zip
				</button>
				<button
					type="button"
					class="btn btn-ghost btn-sm justify-start text-danger"
					role="menuitem"
					onclick={() => run(ondelete)}
				>
					Delete voice
				</button>
			</div>
		</details>
	</div>

	{#if voice.designInstruct}
		<p class="mt-2 line-clamp-2 text-[11px] leading-4 text-ink-secondary">{voice.designInstruct}</p>
	{/if}

	<dl class="metric mt-3 grid grid-cols-3 gap-2 border-t border-rule pt-2 text-[11px]">
		<div>
			<dt class="text-[10px] text-ink-muted">Length</dt>
			<dd class="text-ink">{formatClock(voice.durationSec)}</dd>
		</div>
		<div>
			<dt class="text-[10px] text-ink-muted">Samples</dt>
			<dd class="text-ink">{voice.sampleCount}</dd>
		</div>
		<div>
			<dt class="text-[10px] text-ink-muted">Warnings</dt>
			<dd class={voice.warnings.length > 0 ? 'text-warning' : 'text-ink'}>
				{voice.warnings.length > 0 ? voice.warnings.length : '—'}
			</dd>
		</div>
	</dl>

	{#if !compatible}
		<p class="mt-2 text-[11px] leading-4 text-ink-muted">
			No installed engine can speak this voice yet.
		</p>
	{/if}

	<div class="mt-3 flex items-center gap-1.5">
		<button type="button" class="btn btn-sm flex-1" disabled={previewing} onclick={onpreview}>
			<Play class="size-3" aria-hidden="true" />
			{previewing ? 'Rendering…' : 'Preview'}
		</button>
		<button type="button" class="btn btn-sm btn-primary flex-1" onclick={onuse}>
			Use in Speak
		</button>
	</div>
</article>
