<script lang="ts">
	import Trash2 from '@lucide/svelte/icons/trash-2';
	import { Button } from '#lib/components/ui/button/index.js';
	import StatusBadge from '#lib/components/studio/status-badge.svelte';
	import { formatClock, formatDate, shortText } from '#lib/format.js';
	import { initials, voiceTone, type VoiceItem, type VoiceTone } from '#lib/voices.js';

	let {
		item,
		onUse,
		onDelete
	}: {
		item: VoiceItem;
		onUse: () => void;
		onDelete?: () => Promise<void>;
	} = $props();

	let confirming = $state(false);
	let deleting = $state(false);
	let failed = $state('');

	const voice = $derived(item.kind === 'voice' ? item.voice : null);
	const preset = $derived(item.kind === 'preset' ? item.preset : null);
	const name = $derived(preset ? preset.speaker.replaceAll('_', ' ') : (voice?.name ?? 'Voice'));
	const subtitle = $derived(
		preset
			? `${preset.engineName} · ${preset.modelAlias}`
			: voice
				? shortText(voice.designInstruct || voice.refText || 'No reference text', 80)
				: ''
	);
	const tone = $derived(voiceTone(name));
	const toneClass: Record<VoiceTone, string> = {
		cobalt: 'bg-primary/15 text-primary ring-primary/20',
		navy: 'bg-[#0E2B49] text-[#C7D7E6] ring-[#2A4863]',
		steel: 'bg-elevated-hover text-text-secondary ring-border',
		amber: 'bg-warning/10 text-warning ring-warning/20'
	};

	async function remove(): Promise<void> {
		if (!onDelete) return;
		deleting = true;
		failed = '';
		try {
			await onDelete();
		} catch (error) {
			failed = error instanceof Error ? error.message : 'Could not delete this voice.';
			confirming = false;
		} finally {
			deleting = false;
		}
	}
</script>

<div
	class="grid gap-3 rounded-xl border border-border bg-elevated p-3 transition-colors hover:bg-elevated-hover/45 lg:grid-cols-[minmax(0,1fr)_88px_86px_112px_auto] lg:items-center"
>
	<div class="flex min-w-0 items-center gap-3">
		<span
			class={`flex size-8 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ring-1 ${toneClass[tone]}`}
			aria-hidden="true"
		>
			{initials(name)}
		</span>
		<div class="min-w-0">
			<div class="flex min-w-0 items-center gap-1.5">
				<p class="truncate text-[13px] font-medium text-foreground">{name}</p>
				<StatusBadge
					tone={preset ? 'neutral' : voice?.source === 'designed' ? 'accent' : 'neutral'}
					label={preset ? 'Preset' : voice?.source === 'designed' ? 'Designed' : 'Cloned'}
				/>
			</div>
			<p class="mt-0.5 truncate text-[11px] text-text-secondary" title={subtitle}>{subtitle}</p>
			{#if failed}
				<p class="mt-1 text-[10px] text-danger">{failed}</p>
			{/if}
		</div>
	</div>

	<div class="flex items-center justify-between gap-2 lg:block">
		<span class="text-[10px] text-text-muted uppercase lg:hidden">Duration</span>
		<span class="font-mono text-[11px] text-text-secondary tabular-nums">
			{voice ? formatClock(voice.durationSec) : '—'}
		</span>
	</div>

	<div class="flex items-center justify-between gap-2 lg:block">
		<span class="text-[10px] text-text-muted uppercase lg:hidden">Added</span>
		<span class="font-mono text-[10px] text-text-secondary tabular-nums">
			{voice ? formatDate(voice.createdAt) : 'Built-in'}
		</span>
	</div>

	<div class="flex items-center justify-between gap-2 lg:block">
		<span class="text-[10px] text-text-muted uppercase lg:hidden">Detail</span>
		<span class="font-mono text-[10px] text-text-muted">
			{voice ? (voice.xVectorOnly ? 'x-vector' : 'reference') : preset?.speaker}
		</span>
	</div>

	<div class="flex items-center justify-end gap-1.5">
		<Button variant="outline" size="xs" onclick={onUse}>Use in Speak</Button>
		{#if onDelete}
			{#if confirming}
				<Button variant="destructive" size="xs" disabled={deleting} onclick={() => void remove()}>
					{deleting ? 'Deleting' : 'Delete'}
				</Button>
				<Button variant="ghost" size="xs" disabled={deleting} onclick={() => (confirming = false)}>
					Cancel
				</Button>
			{:else}
				<button
					type="button"
					class="flex size-7 items-center justify-center rounded-md text-text-muted hover:bg-destructive/10 hover:text-destructive focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none"
					aria-label={`Delete ${name}`}
					title={`Delete ${name}`}
					onclick={() => (confirming = true)}
				>
					<Trash2 class="size-3.5" aria-hidden="true" />
				</button>
			{/if}
		{/if}
	</div>
</div>
