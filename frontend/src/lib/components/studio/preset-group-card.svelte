<script lang="ts">
	import ChevronDown from '@lucide/svelte/icons/chevron-down';
	import Mic from '@lucide/svelte/icons/mic';
	import { Button } from '#lib/components/ui/button/index.js';
	import StatusBadge from '#lib/components/studio/status-badge.svelte';
	import { voiceTone, type PresetGroup, type VoiceTone } from '#lib/voices.js';

	let { group, onUse }: { group: PresetGroup; onUse: (speaker: string) => void } = $props();

	let speaker = $state('');
	const tone = $derived(voiceTone(group.modelAlias));

	$effect(() => {
		if (!speaker || !group.speakers.includes(speaker)) {
			speaker = group.speakers[0] ?? '';
		}
	});
	const toneClass: Record<VoiceTone, string> = {
		cobalt: 'bg-primary/15 text-primary ring-primary/20',
		navy: 'bg-[#0E2B49] text-[#C7D7E6] ring-[#2A4863]',
		steel: 'bg-elevated-hover text-text-secondary ring-border',
		amber: 'bg-warning/10 text-warning ring-warning/20'
	};
</script>

<div
	class="grid gap-3 rounded-xl border border-border bg-elevated p-3 transition-colors hover:bg-elevated-hover/45 lg:grid-cols-[minmax(0,1fr)_88px_86px_112px_auto] lg:items-center"
>
	<div class="flex min-w-0 items-center gap-3">
		<span
			class={`flex size-8 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ring-1 ${toneClass[tone]}`}
			aria-hidden="true"
		>
			<Mic class="size-3.5" aria-hidden="true" />
		</span>
		<div class="min-w-0">
			<div class="flex min-w-0 items-center gap-1.5">
				<p class="truncate text-[13px] font-medium text-foreground">{group.modelAlias}</p>
				<StatusBadge tone="neutral" label="Preset" />
			</div>
			<p class="mt-0.5 truncate text-[11px] text-text-secondary">
				{group.engineName} · {group.speakers.length} preset speakers
			</p>
		</div>
	</div>

	<div class="flex items-center justify-between gap-2 lg:block">
		<span class="text-[10px] text-text-muted uppercase lg:hidden">Duration</span>
		<span class="font-mono text-[11px] text-text-secondary">—</span>
	</div>
	<div class="flex items-center justify-between gap-2 lg:block">
		<span class="text-[10px] text-text-muted uppercase lg:hidden">Added</span>
		<span class="font-mono text-[10px] text-text-secondary">Built-in</span>
	</div>
	<div class="flex items-center justify-between gap-2 lg:block">
		<span class="text-[10px] text-text-muted uppercase lg:hidden">Detail</span>
		<span class="font-mono text-[10px] text-text-muted">{group.speakers.length}</span>
	</div>

	<div class="flex flex-wrap items-center justify-end gap-1.5">
		<div class="relative min-w-0">
			<select
				class="h-7 max-w-[132px] appearance-none rounded-md border border-border bg-panel pr-6 pl-2 text-[10px] text-text-secondary focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30 focus-visible:outline-none"
				aria-label={`Speaker for ${group.modelAlias}`}
				bind:value={speaker}
			>
				{#each group.speakers as option (option)}
					<option value={option}>{option.replaceAll('_', ' ')}</option>
				{/each}
			</select>
			<ChevronDown
				class="pointer-events-none absolute top-1/2 right-1.5 size-3 -translate-y-1/2 text-text-muted"
				aria-hidden="true"
			/>
		</div>
		<Button variant="outline" size="xs" onclick={() => onUse(speaker)}>Use in Speak</Button>
	</div>
</div>
