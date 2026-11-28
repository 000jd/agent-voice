<script lang="ts">
	import Play from '@lucide/svelte/icons/play';
	import Plus from '@lucide/svelte/icons/plus';
	import Search from '@lucide/svelte/icons/search';
	import { describeApiError } from '#lib/api/client.js';
	import { previewPreset } from '#lib/api/presets.js';
	import { previewVoice } from '#lib/api/voices.js';
	import type { PresetVoice, VoiceSummary } from '#lib/api/types.js';
	import { clipFromPreview } from '#lib/clips.js';
	import Monogram from '#lib/components/common/monogram.svelte';
	import { formatClock } from '#lib/format.js';
	import { clone } from '#lib/stores/clone.svelte.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';
	import { speak } from '#lib/stores/speak.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { toasts } from '#lib/stores/toasts.svelte.js';
	import {
		PREVIEW_TEXT,
		presetAvailability,
		uniquePresets,
		voiceCompatibility,
		voiceKindLabel
	} from '#lib/voices.js';

	let query = $state('');
	let previewingId = $state<string | null>(null);

	const needle = $derived(query.trim().toLowerCase());

	const voiceRows = $derived(
		studio.voices
			.filter((voice) => {
				if (!needle) return true;
				return (
					voice.name.toLowerCase().includes(needle) ||
					voice.language.toLowerCase().includes(needle) ||
					voice.refText.toLowerCase().includes(needle)
				);
			})
			.map((voice) => ({
				voice,
				selected: speak.config.voiceId === voice.id,
				compatible: voiceCompatibility(voice, studio.engines) !== null
			}))
	);

	const presetRows = $derived(
		uniquePresets(studio.presets)
			.filter((preset) => {
				if (!needle) return true;
				return (
					preset.displayName.toLowerCase().includes(needle) ||
					preset.engineId.toLowerCase().includes(needle) ||
					preset.modelAlias.toLowerCase().includes(needle)
				);
			})
			.map((preset) => ({
				preset,
				selected: speak.config.presetId === preset.presetId,
				availability: presetAvailability(studio.engines, preset)
			}))
	);

	function voiceMeta(voice: VoiceSummary): string {
		return `${voiceKindLabel(voice)} · ${voice.language || 'Auto'} · ${formatClock(voice.durationSec)}`;
	}

	async function previewVoiceItem(voice: VoiceSummary): Promise<void> {
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

	async function previewPresetItem(preset: PresetVoice): Promise<void> {
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
</script>

<div class="flex h-full min-h-0 flex-col">
	<div class="shrink-0 p-2">
		<div class="relative">
			<Search
				class="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-ink-muted"
				aria-hidden="true"
			/>
			<label class="sr-only" for="speak-voice-search">Search voices</label>
			<input
				id="speak-voice-search"
				class="input h-8 pl-7 text-[12px]"
				type="search"
				placeholder="Search voices"
				bind:value={query}
			/>
		</div>
	</div>

	<div class="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
		<section aria-labelledby="picker-your-voices">
			<h2
				id="picker-your-voices"
				class="sticky top-0 z-10 bg-panel px-1 py-1.5 text-[12px] font-medium text-ink-secondary"
			>
				Your voices <span class="metric text-[11px] text-ink-muted">{voiceRows.length}</span>
			</h2>
			{#if studio.catalogLoading && studio.voices.length === 0}
				<div class="space-y-1.5 px-1">
					{#each Array.from({ length: 4 }, (_, index) => index) as index (index)}
						<div class="skeleton h-9 w-full"></div>
					{/each}
				</div>
			{:else if voiceRows.length === 0}
				<p class="px-1 py-2 text-[12px] leading-4 text-ink-muted">
					{studio.voices.length === 0
						? 'No voices yet. Clone one to speak with your own timbre.'
						: 'No voice matches this search.'}
				</p>
			{:else}
				<ul class="space-y-0.5">
					{#each voiceRows as row (row.voice.id)}
						<li class="group flex items-center gap-0.5 rounded-control">
							<button
								type="button"
								class={`flex min-w-0 flex-1 items-center gap-2 rounded-control px-1.5 py-1.5 text-left ${
									row.selected ? 'bg-signal-soft ring-1 ring-signal' : 'hover:bg-surface-hover'
								}`}
								aria-pressed={row.selected}
								onclick={() => speak.selectVoice(row.voice)}
							>
								<Monogram name={row.voice.name} size="sm" />
								<span class="min-w-0 flex-1">
									<span class="block truncate text-[12px] text-ink">{row.voice.name}</span>
									<span class="block truncate text-[12px] text-ink-muted"
										>{voiceMeta(row.voice)}</span
									>
								</span>
								{#if !row.compatible}
									<span class="chip shrink-0">no engine</span>
								{/if}
							</button>
							<button
								type="button"
								class="btn btn-ghost btn-icon shrink-0 opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
								aria-label={`Preview ${row.voice.name}`}
								disabled={previewingId === row.voice.id}
								onclick={() => void previewVoiceItem(row.voice)}
							>
								<Play class="size-3" aria-hidden="true" />
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<section aria-labelledby="picker-presets" class="mt-3">
			<h2
				id="picker-presets"
				class="sticky top-0 z-10 bg-panel px-1 py-1.5 text-[12px] font-medium text-ink-secondary"
			>
				Presets <span class="metric text-[11px] text-ink-muted">{presetRows.length}</span>
			</h2>
			{#if presetRows.length === 0}
				<p class="px-1 py-2 text-[12px] leading-4 text-ink-muted">
					No preset speakers are declared by the installed engines.
				</p>
			{:else}
				<ul class="space-y-0.5">
					{#each presetRows as row (row.preset.presetId)}
						<li class="group flex items-center gap-0.5 rounded-control">
							<button
								type="button"
								class={`flex min-w-0 flex-1 items-center gap-2 rounded-control px-1.5 py-1.5 text-left ${
									row.selected ? 'bg-signal-soft ring-1 ring-signal' : 'hover:bg-surface-hover'
								}`}
								aria-pressed={row.selected}
								disabled={row.availability === 'unavailable'}
								onclick={() => speak.selectPreset(row.preset)}
							>
								<Monogram name={row.preset.displayName} size="sm" kind="preset" />
								<span class="min-w-0 flex-1">
									<span class="block truncate text-[12px] text-ink">{row.preset.displayName}</span>
									<span class="block truncate text-[12px] text-ink-muted">
										{row.preset.engineId} · {row.preset.speaker}
									</span>
								</span>
							</button>
							<button
								type="button"
								class="btn btn-ghost btn-icon shrink-0 opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
								aria-label={`Preview ${row.preset.displayName}`}
								disabled={previewingId === row.preset.presetId ||
									row.availability === 'unavailable'}
								onclick={() => void previewPresetItem(row.preset)}
							>
								<Play class="size-3" aria-hidden="true" />
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	</div>

	<div class="shrink-0 border-t border-rule p-2">
		<button
			type="button"
			class="btn btn-sm w-full justify-start gap-1.5"
			onclick={() => clone.begin('create')}
		>
			<Plus class="size-3.5" aria-hidden="true" />
			Clone a voice
		</button>
	</div>
</div>
