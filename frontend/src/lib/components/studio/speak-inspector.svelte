<script lang="ts">
	import { resolve } from '$app/paths';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { ALL_MODES, MODE_LABELS, modelsForMode, modesForEngine, ttsEngines } from '#lib/speak.js';
	import type { ModelMode, SpeakConfig } from '#lib/api/types.js';
	import SessionList from './session-list.svelte';
	import SegmentedControl from './segmented-control.svelte';
	import SelectField from './select-field.svelte';
	import RangeField from './range-field.svelte';
	import SwitchField from './switch-field.svelte';

	type Tab = 'settings' | 'session';

	let tab = $state<Tab>('settings');

	const engine = $derived(studio.engines.find((item) => item.id === studio.speak.engineId) ?? null);
	const model = $derived(
		engine?.models.find((item) => item.alias === studio.speak.modelAlias) ?? null
	);
	const availableModes = $derived(modesForEngine(engine));
	const cloneModels = $derived(modelsForMode(engine, 'clone'));
	const voiceOptions = $derived(
		studio.voices.length > 0
			? studio.voices.map((voice) => ({
					value: voice.id,
					label: `${voice.name} · ${voice.source === 'designed' ? 'designed' : 'cloned'}`
				}))
			: [{ value: '', label: 'No registered voices', disabled: true }]
	);
	const speakerOptions = $derived(
		(model?.presetSpeakers.length ?? 0) > 0
			? (model?.presetSpeakers ?? []).map((speaker) => ({
					value: speaker,
					label: speaker.replaceAll('_', ' ')
				}))
			: [{ value: '', label: 'No preset speakers', disabled: true }]
	);
	const languageOptions = $derived(
		(engine?.languages.length ?? 0) > 0
			? (engine?.languages ?? []).map((language) => ({ value: language, label: language }))
			: [{ value: 'Auto', label: 'Auto' }]
	);
	const modeOptions = $derived(
		ALL_MODES.map((mode) => ({
			value: mode,
			label: mode === 'custom_voice' ? 'Preset' : mode === 'voice_design' ? 'Design' : 'Clone',
			disabled: !availableModes.includes(mode),
			title: availableModes.includes(mode)
				? MODE_LABELS[mode]
				: `Not offered by this engine (${MODE_LABELS[mode]})`
		}))
	);
	const engineOptions = $derived(
		ttsEngines(studio.engines).map((item) => ({
			value: item.id,
			label: `${item.displayName}${item.installed ? '' : ' · not installed'}`,
			disabled: !item.installed
		}))
	);
	const modelOptions = $derived(
		modelsForMode(engine, studio.speak.mode).map((item) => ({
			value: item.alias,
			label: `${item.alias} · ${item.qualityTier}${item.downloaded ? '' : ' · not downloaded'}`,
			disabled: !item.downloaded
		}))
	);
	const showInstruct = $derived(
		studio.speak.mode === 'voice_design' ||
			(studio.speak.mode === 'custom_voice' && Boolean(model?.supportsInstruct))
	);
</script>

<div class="rounded-xl border border-border bg-elevated">
	<div class="border-b border-border p-3">
		<SegmentedControl
			options={[
				{ value: 'settings', label: 'Settings' },
				{
					value: 'session',
					label: `Session${studio.sessionClips.length ? ` · ${studio.sessionClips.length}` : ''}`
				}
			]}
			value={tab}
			onchange={(next: Tab) => (tab = next)}
			label="Inspector view"
		/>
	</div>

	{#if tab === 'settings'}
		<div class="space-y-3.5 p-3.5">
			<div>
				<span
					class="mb-1 block text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase"
				>
					Voice mode
				</span>
				<SegmentedControl
					options={modeOptions}
					value={studio.speak.mode}
					onchange={(next: ModelMode) => studio.selectMode(next)}
					label="Voice mode"
				/>
			</div>

			<SelectField
				id="speak-engine"
				label="Engine"
				value={studio.speak.engineId}
				options={engineOptions}
				onchange={(value: string) => studio.selectEngine(value)}
				hint={engine?.whyUnavailable ?? undefined}
			/>

			<SelectField
				id="speak-model"
				label="Model"
				value={studio.speak.modelAlias}
				options={modelOptions}
				onchange={(value: string) => studio.selectModel(value)}
				hint={model && !model.downloaded
					? 'Download this checkpoint with the CLI before use.'
					: undefined}
			/>

			{#if studio.speak.mode === 'clone'}
				<SelectField
					id="speak-voice"
					label="Voice"
					value={studio.speak.voiceId}
					options={voiceOptions}
					onchange={(value: string) => (studio.speak = { ...studio.speak, voiceId: value })}
					hint={cloneModels.length === 0 ? 'This engine has no clone model.' : undefined}
				/>
				{#if studio.voices.length === 0}
					<p class="text-[10px] leading-4 text-text-muted">
						No registered voices. Add one in <a
							class="text-primary hover:underline"
							href={resolve('voices')}>Voices</a
						>.
					</p>
				{/if}
			{:else if studio.speak.mode === 'custom_voice'}
				<SelectField
					id="speak-speaker"
					label="Preset speaker"
					value={studio.speak.speaker}
					options={speakerOptions}
					onchange={(value: string) => (studio.speak = { ...studio.speak, speaker: value })}
				/>
			{/if}

			{#if showInstruct}
				<div>
					<div class="mb-1 flex items-center justify-between gap-2">
						<label
							for="speak-instruct"
							class="text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase"
						>
							{studio.speak.mode === 'voice_design' ? 'Voice description' : 'Delivery'}
						</label>
						<span class="font-mono text-[10px] text-text-muted tabular-nums">
							{studio.speak.instruct.length} / 1000
						</span>
					</div>
					<textarea
						id="speak-instruct"
						class="min-h-[76px] w-full resize-y rounded-lg border border-border bg-panel px-2.5 py-2 text-[12px] leading-5 text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
						maxlength="1000"
						placeholder={studio.speak.mode === 'voice_design'
							? 'warm baritone, measured pace, late-night radio'
							: 'slightly slower, close and intimate'}
						value={studio.speak.instruct}
						oninput={(event) =>
							(studio.speak = { ...studio.speak, instruct: event.currentTarget.value })}></textarea>
				</div>
			{/if}

			<div class="grid grid-cols-2 gap-2.5">
				<SelectField
					id="speak-language"
					label="Language"
					value={studio.speak.language}
					options={languageOptions}
					onchange={(value: string) => (studio.speak = { ...studio.speak, language: value })}
				/>
				<SelectField
					id="speak-format"
					label="Format"
					value={studio.speak.format}
					options={[
						{ value: 'wav', label: 'WAV' },
						{ value: 'mp3', label: 'MP3' },
						{ value: 'opus', label: 'Opus' }
					]}
					onchange={(value: string) =>
						(studio.speak = { ...studio.speak, format: value as SpeakConfig['format'] })}
				/>
			</div>

			<RangeField
				id="speak-speed"
				label="Speed"
				value={studio.speak.speed}
				min={0.5}
				max={2}
				step={0.05}
				display={`${studio.speak.speed.toFixed(2)}×`}
				oninput={(value: number) => (studio.speak = { ...studio.speak, speed: value })}
			/>

			<div class="grid grid-cols-2 gap-2.5">
				<div>
					<label
						for="speak-take"
						class="mb-1 block text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase"
					>
						Take
					</label>
					<input
						id="speak-take"
						class="h-8 w-full rounded-lg border border-border bg-panel px-2.5 font-mono text-[12px] text-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
						type="number"
						min="1"
						placeholder="1"
						value={studio.speak.take ?? ''}
						oninput={(event) =>
							(studio.speak = {
								...studio.speak,
								take: event.currentTarget.value ? Number(event.currentTarget.value) : null
							})}
					/>
				</div>
				<div>
					<label
						for="speak-seed"
						class="mb-1 block text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase"
					>
						Seed
					</label>
					<input
						id="speak-seed"
						class="h-8 w-full rounded-lg border border-border bg-panel px-2.5 font-mono text-[12px] text-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
						type="number"
						placeholder="auto"
						value={studio.speak.seed ?? ''}
						oninput={(event) =>
							(studio.speak = {
								...studio.speak,
								seed: event.currentTarget.value ? Number(event.currentTarget.value) : null
							})}
					/>
				</div>
			</div>

			<details class="group rounded-lg border border-border bg-panel">
				<summary
					class="cursor-pointer list-none px-2.5 py-2 text-[11px] font-medium text-text-secondary hover:text-foreground"
				>
					Advanced
				</summary>
				<div class="space-y-1 border-t border-border px-2.5 py-1.5">
					<SwitchField
						label="Markup"
						description="Interpret [pause], [slow], [emphasis] and [spell]."
						checked={studio.speak.markup}
						onchange={(checked: boolean) => (studio.speak = { ...studio.speak, markup: checked })}
					/>
					<SwitchField
						label="Spell identifiers"
						description="Expand code-like tokens before synthesis."
						checked={studio.speak.spellIdentifiers}
						onchange={(checked: boolean) =>
							(studio.speak = { ...studio.speak, spellIdentifiers: checked })}
					/>
					<RangeField
						id="speak-gap"
						label="Paragraph gap"
						value={studio.speak.paragraphGapMs}
						min={0}
						max={5000}
						step={50}
						display={`${studio.speak.paragraphGapMs} ms`}
						oninput={(value: number) => (studio.speak = { ...studio.speak, paragraphGapMs: value })}
					/>
				</div>
			</details>
		</div>
	{:else}
		<div class="px-3.5">
			<SessionList clips={studio.sessionClips} />
		</div>
	{/if}
</div>
