<script lang="ts">
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import Play from '@lucide/svelte/icons/play';
	import Dialog from '#lib/components/common/dialog.svelte';
	import ErrorNotice from '#lib/components/common/error-notice.svelte';
	import { designVoice } from '#lib/api/speak.js';
	import type { ApiErrorPayload, DesignResult } from '#lib/api/types.js';
	import { describeApiError } from '#lib/api/client.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { formatClock } from '#lib/format.js';

	let { open = $bindable(false) }: { open?: boolean } = $props();

	const DEFAULT_SAMPLE =
		'Welcome. Today we are going to break this concept down, step by step, clearly and intuitively.';

	let name = $state('');
	let instruct = $state('');
	let sampleText = $state(DEFAULT_SAMPLE);
	let language = $state('English');
	let candidates = $state(1);
	let busy = $state(false);
	let error = $state<ApiErrorPayload | null>(null);
	let result = $state<DesignResult | null>(null);

	const designEngine = $derived(
		studio.ttsEngines.find(
			(engine) => engine.installed && engine.models.some((model) => model.mode === 'voice_design')
		) ?? null
	);
	const designModel = $derived(
		designEngine?.models.find((model) => model.mode === 'voice_design' && model.downloaded) ??
			designEngine?.models.find((model) => model.mode === 'voice_design') ??
			null
	);
	const languages = $derived(
		designEngine && designEngine.languages.length > 0 ? designEngine.languages : ['English']
	);
	const canSubmit = $derived(
		name.trim().length > 0 &&
			name.trim().length <= 64 &&
			instruct.trim().length > 0 &&
			designModel !== null &&
			!busy
	);

	function reset(): void {
		name = '';
		instruct = '';
		sampleText = DEFAULT_SAMPLE;
		language = 'English';
		candidates = 1;
		busy = false;
		error = null;
		result = null;
	}

	async function submit(): Promise<void> {
		if (!canSubmit || !designEngine || !designModel) return;
		busy = true;
		error = null;
		try {
			result = await designVoice({
				name: name.trim(),
				instruct: instruct.trim(),
				sampleText: sampleText.trim() || DEFAULT_SAMPLE,
				language,
				saveAsClone: true,
				candidates,
				take: null,
				seed: null,
				engine: designEngine.id,
				model: designModel.alias,
				quality: 'high',
				overwrite: false
			});
			await studio.refreshVoices();
		} catch (caught) {
			error = describeApiError(caught);
		} finally {
			busy = false;
		}
	}

	function play(index: number): void {
		if (!result) return;
		const candidate = result.candidates[index];
		if (!candidate || !candidate.downloadUrl) return;
		void getPlayer().play({
			id: candidate.downloadUrl,
			title: `${result.name} · take ${candidate.take}`,
			subtitle: `${result.engineId} · ${result.modelAlias}`,
			url: candidate.downloadUrl,
			durationSec: candidate.durationSec,
			engineId: result.engineId,
			modelAlias: result.modelAlias,
			cached: false,
			elapsedMs: 0,
			realtimeMultiple: null,
			format: 'wav',
			createdAt: Date.now() / 1000,
			voiceId: result.voiceId,
			speaker: null,
			mode: 'voice_design'
		});
	}
</script>

<Dialog
	bind:open
	title="Design a voice"
	description="Describe a timbre in words; a VoiceDesign checkpoint renders it and saves the result as a voice."
	size="md"
	onclose={reset}
>
	{#if result}
		<div class="space-y-4">
			<div class="rounded-control border border-rule bg-surface p-4">
				<p class="text-[14px] font-medium text-ink">“{result.name}” is saved</p>
				<p class="mt-1 text-[12px] leading-5 text-ink-secondary">
					Designed with {result.engineId} · {result.modelAlias}. It is in Your voices and ready to
					speak.
				</p>
			</div>
			{#if result.candidates.length > 0}
				<div>
					<h3 class="mb-1.5 text-[12px] font-medium text-ink-secondary">Audition takes</h3>
					<ul class="divide-y divide-rule rounded-control border border-rule">
						{#each result.candidates as candidate, index (candidate.take)}
							<li class="flex items-center justify-between gap-3 px-3 py-2">
								<span class="metric text-[11px] text-ink-secondary">
									Take {candidate.take} · {formatClock(candidate.durationSec)}
									{#if candidate.seed !== null}· seed {candidate.seed}{/if}
								</span>
								<button type="button" class="btn btn-sm" onclick={() => play(index)}>
									<Play class="size-3" aria-hidden="true" />
									Play
								</button>
							</li>
						{/each}
					</ul>
				</div>
			{:else}
				<p class="text-[12px] text-ink-secondary">
					The engine returned a preview without per-take URLs; open the voice in Voices to preview
					it.
				</p>
			{/if}
			<button
				type="button"
				class="btn"
				onclick={() => {
					reset();
					open = false;
				}}
			>
				Design another
			</button>
		</div>
	{:else if !designEngine || !designModel}
		<div class="space-y-3">
			<ErrorNotice
				error={{
					status: 0,
					code: 'NO_DESIGN_ENGINE',
					message: designEngine
						? 'The design model is not downloaded.'
						: 'No installed engine can design voices.',
					suggestion:
						'Install Qwen3-TTS (voice_design) on Models, download its VoiceDesign checkpoint, then return here.',
					correlationId: ''
				}}
			/>
			<button
				type="button"
				class="btn"
				onclick={() => {
					open = false;
					void goto(resolve('models'));
				}}
			>
				Open Models
			</button>
		</div>
	{:else}
		<div class="space-y-3">
			<div class="space-y-1">
				<label class="field-label" for="design-name">Voice name</label>
				<input
					id="design-name"
					class="input"
					type="text"
					maxlength="64"
					bind:value={name}
					placeholder="Midnight narrator"
				/>
			</div>
			<div class="space-y-1">
				<label class="field-label" for="design-instruct">Describe the voice</label>
				<textarea
					id="design-instruct"
					class="textarea min-h-20 text-[13px]"
					bind:value={instruct}
					placeholder="A warm, low, measured male narrator with a slight rasp; close, intimate delivery."
				></textarea>
				<p class="text-[11px] text-ink-muted">
					Age, timbre, delivery, accent and pace all help. Gender and pitch cannot be changed after
					saving.
				</p>
			</div>
			<div class="space-y-1">
				<label class="field-label" for="design-sample">Sentence to speak</label>
				<textarea id="design-sample" class="textarea min-h-16 text-[13px]" bind:value={sampleText}
				></textarea>
			</div>
			<div class="grid gap-3 sm:grid-cols-2">
				<div class="space-y-1">
					<label class="field-label" for="design-language">Language</label>
					<select id="design-language" class="select" bind:value={language}>
						{#each languages as option (option)}
							<option value={option}>{option}</option>
						{/each}
					</select>
				</div>
				<div class="space-y-1">
					<label class="field-label" for="design-candidates">Audition takes</label>
					<select id="design-candidates" class="select" bind:value={candidates}>
						<option value={1}>1 take</option>
						<option value={2}>2 takes</option>
						<option value={3}>3 takes</option>
					</select>
				</div>
			</div>
			<p class="metric text-[11px] text-ink-muted">
				{designEngine.displayName} · {designModel.alias}
			</p>
			{#if error}
				<ErrorNotice {error} />
			{/if}
		</div>
	{/if}

	{#snippet footer()}
		{#if result}
			<button type="button" class="btn btn-primary" onclick={() => (open = false)}>Done</button>
		{:else}
			<button type="button" class="btn" onclick={() => (open = false)}>Cancel</button>
			<button
				type="button"
				class="btn btn-primary"
				disabled={!canSubmit}
				onclick={() => void submit()}
			>
				{busy ? 'Designing…' : 'Design and save'}
			</button>
		{/if}
	{/snippet}
</Dialog>
