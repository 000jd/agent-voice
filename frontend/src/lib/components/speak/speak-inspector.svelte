<script lang="ts">
	import ChevronDown from '@lucide/svelte/icons/chevron-down';
	import Monogram from '#lib/components/common/monogram.svelte';
	import { formatClock } from '#lib/format.js';
	import { MODE_LABELS, findEngine } from '#lib/speak.js';
	import { speak } from '#lib/stores/speak.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { presetId, voiceKindLabel } from '#lib/voices.js';

	const engine = $derived(findEngine(studio.engines, speak.config.engineId));
	const model = $derived(
		engine?.models.find((item) => item.alias === speak.config.modelAlias) ?? null
	);
	const models = $derived(engine?.models ?? []);
	const engines = $derived(studio.ttsEngines);
	const languages = $derived(engine && engine.languages.length > 0 ? engine.languages : ['Auto']);
	const selectedVoice = $derived(
		studio.voices.find((voice) => voice.id === speak.config.voiceId) ?? null
	);
	const selectedPreset = $derived(
		studio.presets.find((preset) => presetId(preset) === speak.config.presetId) ?? null
	);
	const selectionName = $derived(
		selectedVoice?.name ??
			(selectedPreset ? `${selectedPreset.displayName} (preset)` : 'No voice selected')
	);
	const selectionMeta = $derived(
		selectedVoice
			? `${voiceKindLabel(selectedVoice)} · ${selectedVoice.language || 'Auto'} · ${formatClock(selectedVoice.durationSec)}`
			: selectedPreset
				? `${selectedPreset.engineId} · ${selectedPreset.speaker}`
				: 'Choose a voice in the picker'
	);
</script>

<div class="flex h-full min-h-0 flex-col overflow-y-auto">
	<div class="space-y-3 p-3">
		<section aria-labelledby="inspector-voice">
			<h2 id="inspector-voice" class="mb-1.5 text-[12px] font-medium text-ink-secondary">Voice</h2>
			<div class="flex items-center gap-2 rounded-control border border-rule bg-surface px-2 py-2">
				<Monogram name={selectionName} size="sm" kind={selectedPreset ? 'preset' : 'voice'} />
				<div class="min-w-0">
					<p class="truncate text-[12px] font-medium text-ink">{selectionName}</p>
					<p class="truncate text-[11px] text-ink-muted">{selectionMeta}</p>
				</div>
			</div>
		</section>

		<section aria-labelledby="inspector-model" class="border-t border-rule pt-3">
			<h2 id="inspector-model" class="mb-1.5 text-[12px] font-medium text-ink-secondary">
				Engine and model
			</h2>
			<p class="text-[12px] text-ink">
				{engine?.displayName ?? 'No engine'}
				<span class="text-ink-muted">·</span>
				<span class="metric text-[11px]">{model?.alias ?? 'no model'}</span>
			</p>
			<p class="mt-0.5 text-[11px] leading-4 text-ink-muted">
				Chosen automatically from the voice. Override it under Advanced.
			</p>
		</section>

		<section aria-labelledby="inspector-sound" class="space-y-2 border-t border-rule pt-3">
			<h2 id="inspector-sound" class="text-[12px] font-medium text-ink-secondary">
				Language and format
			</h2>
			<div class="space-y-1">
				<label class="field-label" for="inspect-language">Language</label>
				<select
					id="inspect-language"
					class="select h-8 text-[12px]"
					value={speak.config.language}
					onchange={(event) => speak.patch({ language: event.currentTarget.value })}
				>
					{#each languages as language (language)}
						<option value={language}>{language}</option>
					{/each}
				</select>
			</div>
			<div class="grid grid-cols-2 gap-2">
				<div class="space-y-1">
					<label class="field-label" for="inspect-format">Format</label>
					<select
						id="inspect-format"
						class="select h-8 text-[12px]"
						value={speak.config.format}
						onchange={(event) =>
							speak.patch({ format: event.currentTarget.value as 'wav' | 'mp3' | 'opus' | 'flac' })}
					>
						<option value="wav">wav</option>
						<option value="mp3">mp3</option>
						<option value="opus">opus</option>
						<option value="flac">flac</option>
					</select>
				</div>
				<div class="space-y-1">
					<label class="field-label" for="inspect-speed">Speed</label>
					<input
						id="inspect-speed"
						class="input metric h-8 text-[12px]"
						type="number"
						min="0.5"
						max="2"
						step="0.05"
						value={speak.config.speed}
						onchange={(event) =>
							speak.patch({ speed: Math.min(2, Math.max(0.5, Number(event.currentTarget.value))) })}
					/>
				</div>
			</div>
		</section>

		<details class="group border-t border-rule pt-2">
			<summary class="btn btn-ghost btn-sm w-full justify-between">
				Advanced
				<ChevronDown
					class="size-3.5 transition-transform group-open:rotate-180"
					aria-hidden="true"
				/>
			</summary>
			<div class="space-y-2.5 pt-2">
				<div class="space-y-1">
					<label class="field-label" for="inspect-engine">Engine</label>
					<select
						id="inspect-engine"
						class="select h-8 text-[12px]"
						value={speak.config.engineId}
						onchange={(event) => speak.selectEngine(event.currentTarget.value)}
					>
						{#each engines as item (item.id)}
							<option value={item.id}>
								{item.displayName}{item.installed ? '' : ' (not installed)'}
							</option>
						{/each}
					</select>
				</div>
				<div class="space-y-1">
					<label class="field-label" for="inspect-model">Model</label>
					<select
						id="inspect-model"
						class="select h-8 text-[12px]"
						value={speak.config.modelAlias}
						onchange={(event) => speak.selectModel(event.currentTarget.value)}
					>
						{#each models as item (item.alias)}
							<option value={item.alias}>
								{item.alias} · {MODE_LABELS[item.mode]}{item.downloaded ? '' : ' · not downloaded'}
							</option>
						{/each}
					</select>
				</div>
				<p class="text-[11px] text-ink-muted">
					Mode: {MODE_LABELS[speak.config.mode]}
					{#if model && !model.downloaded}
						· weights are not downloaded (Models)
					{/if}
				</p>

				<div class="space-y-1">
					<label class="field-label" for="inspect-instruct">Instruct</label>
					<textarea
						id="inspect-instruct"
						class="textarea min-h-16 text-[12px]"
						placeholder="Optional delivery direction for instruct-capable models."
						disabled={!engine?.instructControl}
						value={speak.config.instruct}
						oninput={(event) => speak.patch({ instruct: event.currentTarget.value })}></textarea>
					{#if !engine?.instructControl}
						<p class="text-[11px] text-ink-muted">This engine does not accept an instruct.</p>
					{/if}
				</div>

				<div class="grid grid-cols-2 gap-2">
					<div class="space-y-1">
						<label class="field-label" for="inspect-quality">Quality</label>
						<select
							id="inspect-quality"
							class="select h-8 text-[12px]"
							value={speak.config.quality}
							onchange={(event) =>
								speak.patch({ quality: event.currentTarget.value === 'fast' ? 'fast' : 'high' })}
						>
							<option value="high">high</option>
							<option value="fast">fast</option>
						</select>
					</div>
					<div class="space-y-1">
						<label class="field-label" for="inspect-take">Take</label>
						<input
							id="inspect-take"
							class="input metric h-8 text-[12px]"
							type="number"
							min="1"
							placeholder="auto"
							value={speak.config.take ?? ''}
							onchange={(event) => {
								const raw = event.currentTarget.value.trim();
								speak.patch({ take: raw ? Math.max(1, Number(raw)) : null });
							}}
						/>
					</div>
				</div>

				<div class="space-y-1">
					<label class="field-label" for="inspect-seed">Seed</label>
					<input
						id="inspect-seed"
						class="input metric h-8 text-[12px]"
						type="number"
						placeholder="random"
						value={speak.config.seed ?? ''}
						onchange={(event) => {
							const raw = event.currentTarget.value.trim();
							speak.patch({ seed: raw ? Number(raw) : null });
						}}
					/>
				</div>

				<label class="flex items-start gap-2 text-[12px] text-ink-secondary">
					<input
						class="checkbox mt-0.5"
						type="checkbox"
						checked={speak.config.markup}
						onchange={(event) => speak.patch({ markup: event.currentTarget.checked })}
					/>
					Interpret markup tags
				</label>
				<label class="flex items-start gap-2 text-[12px] text-ink-secondary">
					<input
						class="checkbox mt-0.5"
						type="checkbox"
						checked={speak.config.spellIdentifiers}
						onchange={(event) => speak.patch({ spellIdentifiers: event.currentTarget.checked })}
					/>
					Spell code-like identifiers
				</label>

				<div class="grid grid-cols-2 gap-2">
					<div class="space-y-1">
						<label class="field-label" for="inspect-gap">Paragraph gap (ms)</label>
						<input
							id="inspect-gap"
							class="input metric h-8 text-[12px]"
							type="number"
							min="0"
							max="5000"
							step="50"
							value={speak.config.paragraphGapMs}
							onchange={(event) =>
								speak.patch({
									paragraphGapMs: Math.min(5000, Math.max(0, Number(event.currentTarget.value)))
								})}
						/>
					</div>
					<div class="space-y-1">
						<label class="field-label" for="inspect-project">Lexicon project</label>
						<input
							id="inspect-project"
							class="input metric h-8 text-[12px]"
							type="text"
							placeholder="none"
							value={speak.config.project}
							oninput={(event) => speak.patch({ project: event.currentTarget.value })}
						/>
					</div>
				</div>
			</div>
		</details>
	</div>
</div>
