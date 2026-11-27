<script lang="ts">
	import Play from '@lucide/svelte/icons/play';
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import { designVoice, describeApiError } from '#lib/api/studio.js';
	import type { ApiErrorPayload, DesignResult } from '#lib/api/types.js';
	import { Button } from '#lib/components/ui/button/index.js';
	import ErrorNotice from './error-notice.svelte';
	import SlideOver from './slide-over.svelte';
	import SelectField from './select-field.svelte';
	import { formatClock } from '#lib/format.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import {
		clipFromCandidate,
		DEFAULT_DESIGN_SAMPLE,
		designRequest,
		findDesignEngine,
		findDesignModel
	} from '#lib/voices.js';

	let {
		open,
		onclose,
		onSaved
	}: { open: boolean; onclose: () => void; onSaved?: (voiceId: string) => void } = $props();

	const player = getPlayer();
	let name = $state('');
	let instruct = $state('');
	let sampleText = $state(DEFAULT_DESIGN_SAMPLE);
	let candidatesCount = $state(2);
	let engineId = $state('');
	let modelAlias = $state('');
	let language = $state('English');
	let result = $state<DesignResult | null>(null);
	let selectedTake = $state<number | null>(null);
	let busy = $state(false);
	let error = $state<ApiErrorPayload | string>('');

	const designEngine = $derived(studio.engines.find((engine) => engine.id === engineId) ?? null);
	const designModel = $derived(
		designEngine?.models.find((model) => model.alias === modelAlias) ?? null
	);
	const engineOptions = $derived(
		studio.engines
			.filter((engine) => engine.kind === 'tts' && engine.installed)
			.filter((engine) => engine.models.some((model) => model.mode === 'voice_design'))
			.map((engine) => ({ value: engine.id, label: engine.displayName }))
	);
	const modelOptions = $derived(
		(designEngine?.models ?? [])
			.filter((model) => model.mode === 'voice_design')
			.map((model) => ({
				value: model.alias,
				label: `${model.alias}${model.downloaded ? '' : ' · not downloaded'}`,
				disabled: !model.downloaded
			}))
	);
	const languageOptions = $derived(
		(designEngine?.languages.length ?? 0) > 0
			? (designEngine?.languages ?? []).map((item) => ({ value: item, label: item }))
			: [{ value: 'English', label: 'English' }]
	);
	const selectedCandidate = $derived(
		result?.candidates.find((candidate) => candidate.take === selectedTake) ?? null
	);

	$effect(() => {
		if (!open) return;
		if (!engineId) {
			const engine = findDesignEngine(studio.engines);
			engineId = engine?.id ?? '';
			modelAlias = findDesignModel(engine);
			language = engine?.languages.includes('English')
				? 'English'
				: (engine?.languages[0] ?? 'English');
		}
	});

	function resetResult(): void {
		result = null;
		selectedTake = null;
		error = '';
	}

	function setEngine(value: string): void {
		const engine = studio.engines.find((item) => item.id === value);
		engineId = value;
		modelAlias = findDesignModel(engine ?? null);
		language = engine?.languages.includes('English')
			? 'English'
			: (engine?.languages[0] ?? 'English');
		resetResult();
	}

	function validate(): string | null {
		if (!name.trim()) return 'Give the voice a name.';
		if (!instruct.trim()) return 'Describe the voice you want.';
		if (instruct.length > 1000) return 'Instruct is limited to 1,000 characters.';
		if (sampleText.length > 500) return 'Sample text is limited to 500 characters.';
		if (!engineId || !modelAlias) return 'Choose a voice-design engine and model.';
		return null;
	}

	async function audition(): Promise<void> {
		const invalid = validate();
		if (invalid) {
			error = invalid;
			return;
		}
		if (candidatesCount === 1) {
			await saveDirect();
			return;
		}
		busy = true;
		error = '';
		result = null;
		selectedTake = null;
		try {
			result = await designVoice(
				designRequest({
					name,
					instruct,
					sampleText,
					language,
					candidates: candidatesCount,
					engineId,
					modelAlias,
					saveAsClone: false
				})
			);
			selectedTake = result.candidates[0]?.take ?? null;
		} catch (cause) {
			error = describeApiError(cause);
		} finally {
			busy = false;
		}
	}

	async function saveDirect(): Promise<void> {
		const invalid = validate();
		if (invalid) {
			error = invalid;
			return;
		}
		busy = true;
		error = '';
		try {
			const saved = await designVoice(
				designRequest({
					name,
					instruct,
					sampleText,
					language,
					candidates: 1,
					engineId,
					modelAlias,
					saveAsClone: true
				})
			);
			await finish(saved);
		} catch (cause) {
			error = describeApiError(cause);
		} finally {
			busy = false;
		}
	}

	async function saveSelected(): Promise<void> {
		if (!result || !selectedCandidate) return;
		busy = true;
		error = '';
		try {
			const saved = await designVoice(
				designRequest({
					name,
					instruct,
					sampleText,
					language,
					candidates: 1,
					engineId: result.engineId,
					modelAlias: result.modelAlias,
					take: selectedCandidate.take,
					seed: selectedCandidate.seed,
					saveAsClone: true
				})
			);
			await finish(saved);
		} catch (cause) {
			error = describeApiError(cause);
		} finally {
			busy = false;
		}
	}

	async function finish(saved: DesignResult): Promise<void> {
		await studio.refreshVoices();
		if (saved.voiceId) onSaved?.(saved.voiceId);
		onclose();
	}

	function playCandidate(candidate: DesignResult['candidates'][number]): void {
		if (!candidate.downloadUrl) return;
		void player.play(
			clipFromCandidate(
				candidate,
				name || 'Designed voice',
				result?.engineId ?? engineId,
				result?.modelAlias ?? modelAlias
			)
		);
	}
</script>

<SlideOver
	{open}
	title="Design a voice"
	description="Describe the timbre, audition a few takes, then save the one you want to reuse."
	{onclose}
>
	<div class="space-y-3.5">
		{#if error}
			<ErrorNotice {error} />
		{/if}

		<div>
			<label
				for="design-name"
				class="mb-1 block text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase"
			>
				Voice name
			</label>
			<input
				id="design-name"
				class="h-8 w-full rounded-lg border border-border bg-elevated px-2.5 text-[12px] text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
				placeholder="narrator-warm"
				bind:value={name}
			/>
		</div>

		<div>
			<div class="mb-1 flex items-center justify-between gap-2">
				<label
					for="design-instruct"
					class="text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase"
				>
					Description
				</label>
				<span class="font-mono text-[10px] text-text-muted tabular-nums">
					{instruct.length} / 1000
				</span>
			</div>
			<textarea
				id="design-instruct"
				class="min-h-[96px] w-full resize-y rounded-lg border border-border bg-elevated px-2.5 py-2 text-[12px] leading-5 text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
				maxlength="1000"
				placeholder="warm baritone, measured pace, late-night radio"
				bind:value={instruct}></textarea>
		</div>

		<div>
			<div class="mb-1 flex items-center justify-between gap-2">
				<label
					for="design-sample"
					class="text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase"
				>
					Sample text
				</label>
				<span class="font-mono text-[10px] text-text-muted tabular-nums">
					{sampleText.length} / 500
				</span>
			</div>
			<textarea
				id="design-sample"
				class="min-h-[64px] w-full resize-y rounded-lg border border-border bg-elevated px-2.5 py-2 text-[12px] leading-5 text-foreground placeholder:text-text-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/20 focus-visible:outline-none"
				maxlength="500"
				bind:value={sampleText}></textarea>
		</div>

		<div class="grid grid-cols-2 gap-2.5">
			<SelectField
				id="design-engine"
				label="Engine"
				value={engineId}
				options={engineOptions}
				onchange={setEngine}
				hint={engineOptions.length === 0 ? 'No installed voice-design engine.' : undefined}
			/>
			<SelectField
				id="design-model"
				label="Model"
				value={modelAlias}
				options={modelOptions}
				onchange={(value: string) => {
					modelAlias = value;
					resetResult();
				}}
				hint={designModel && !designModel.downloaded
					? 'Download this checkpoint with the CLI.'
					: undefined}
			/>
		</div>

		<div class="grid grid-cols-2 gap-2.5">
			<SelectField
				id="design-language"
				label="Language"
				value={language}
				options={languageOptions}
				onchange={(value: string) => (language = value)}
			/>
			<SelectField
				id="design-candidates"
				label="Takes"
				value={String(candidatesCount)}
				options={[
					{ value: '1', label: '1 · save directly' },
					{ value: '2', label: '2 · audition' },
					{ value: '3', label: '3 · audition' }
				]}
				onchange={(value: string) => {
					candidatesCount = Number(value);
					resetResult();
				}}
			/>
		</div>

		{#if result && result.candidates.length > 0}
			<div class="space-y-2">
				<p class="text-[10px] font-medium tracking-[0.06em] text-text-muted uppercase">
					Audition takes
				</p>
				{#each result.candidates as candidate (candidate.take)}
					<div
						class={`flex items-center gap-2 rounded-lg border p-2 ${selectedTake === candidate.take ? 'border-primary/40 bg-primary/5' : 'border-border bg-panel'}`}
					>
						<button
							type="button"
							class="flex size-7 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground disabled:opacity-40"
							aria-label={`Play take ${candidate.take}`}
							disabled={!candidate.downloadUrl}
							onclick={() => playCandidate(candidate)}
						>
							<Play class="size-3 translate-x-px" aria-hidden="true" />
						</button>
						<button
							type="button"
							class="min-w-0 flex-1 text-left"
							aria-pressed={selectedTake === candidate.take}
							onclick={() => (selectedTake = candidate.take)}
						>
							<span class="block text-[11px] font-medium text-foreground"
								>Take {candidate.take}</span
							>
							<span class="block font-mono text-[10px] text-text-muted tabular-nums">
								{formatClock(candidate.durationSec)} · seed {candidate.seed ?? 'auto'}
							</span>
						</button>
						{#if selectedTake === candidate.take}
							<span class="text-[10px] font-medium text-primary">Selected</span>
						{/if}
					</div>
				{/each}
			</div>
		{/if}

		<div class="sticky bottom-0 -mx-4 mt-3 -mb-4 border-t border-border bg-panel px-4 py-3">
			{#if candidatesCount === 1}
				<Button class="h-9 w-full" disabled={busy} onclick={() => void saveDirect()}>
					{#if busy}
						<LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
						Designing
					{:else}
						Design & save
					{/if}
				</Button>
				<p class="mt-1.5 text-center text-[10px] leading-4 text-text-muted">
					A single design returns a local preview path, not a browser-playable URL. The voice will
					appear in the library.
				</p>
			{:else if result}
				<Button
					class="h-9 w-full"
					disabled={busy || !selectedTake}
					onclick={() => void saveSelected()}
				>
					{#if busy}
						<LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
						Saving
					{:else}
						Save selected take
					{/if}
				</Button>
			{:else}
				<Button class="h-9 w-full" disabled={busy} onclick={() => void audition()}>
					{#if busy}
						<LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
						Rendering takes
					{:else}
						Audition takes
					{/if}
				</Button>
			{/if}
		</div>
	</div>
</SlideOver>
