<script lang="ts">
	import Check from '@lucide/svelte/icons/check';
	import Download from '@lucide/svelte/icons/download';
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import type { EngineSummary, Job, ModelSummary } from '#lib/api/types.js';
	import { formatVram } from '#lib/format.js';

	let {
		engine,
		mutationAllowed,
		jobs,
		oninstall,
		ondownload
	}: {
		engine: EngineSummary;
		mutationAllowed: boolean;
		jobs: Record<string, Job>;
		oninstall: (engineId: string, device: string) => void;
		ondownload: (engineId: string, model: ModelSummary) => void;
	} = $props();

	const devices = $derived(engine.supportedDevices.length > 0 ? engine.supportedDevices : ['auto']);
	let chosenDevice = $state<string | null>(null);
	const device = $derived(
		chosenDevice ?? engine.defaultDevice ?? engine.supportedDevices[0] ?? 'auto'
	);

	const installJob = $derived(jobs[`install:${engine.id}`] ?? null);
	const installRunning = $derived(
		installJob !== null && (installJob.status === 'pending' || installJob.status === 'running')
	);
	const cliHint = $derived(`agent-voice engines install ${engine.id} --device ${device}`);

	const MODE_LABELS: Record<ModelSummary['mode'], string> = {
		clone: 'clone',
		custom_voice: 'custom voice',
		voice_design: 'voice design'
	};

	const modelRows = $derived(
		engine.models.map((model) => {
			const job = jobs[`download:${engine.id}:${model.alias}`] ?? null;
			return {
				model,
				job,
				running: job !== null && (job.status === 'pending' || job.status === 'running')
			};
		})
	);
</script>

<section class="card flex flex-col" aria-labelledby={`engine-${engine.id}`}>
	<header class="flex flex-wrap items-start gap-2 border-b border-rule p-3">
		<div class="min-w-0 flex-1">
			<h3
				id={`engine-${engine.id}`}
				class="flex flex-wrap items-center gap-1.5 text-[13px] font-medium text-ink"
			>
				{engine.displayName}
				<span class="chip">{engine.kind.toUpperCase()}</span>
				{#if engine.installed}
					<span class="chip chip-success">Installed</span>
				{:else}
					<span class="chip">Not installed</span>
				{/if}
				{#if engine.resolvedDevice}
					<span class="chip">{engine.resolvedDevice}</span>
				{/if}
			</h3>
			<p class="metric mt-0.5 text-[11px] text-ink-muted">
				{engine.id} · {engine.backend}
				{engine.backendVersion}
				{#if engine.device}· scheduling {engine.device}{/if}
			</p>
		</div>
	</header>

	{#if engine.whyUnavailable}
		<p class="border-b border-rule px-3 py-2 text-[11px] leading-4 text-warning">
			{engine.whyUnavailable}
		</p>
	{/if}

	<div class="flex flex-wrap items-end gap-2 border-b border-rule p-3">
		<div class="space-y-1">
			<label class="field-label" for={`device-${engine.id}`}>Install for device</label>
			<select
				id={`device-${engine.id}`}
				class="select metric h-8 w-[132px] text-[12px]"
				value={device}
				onchange={(event) => (chosenDevice = event.currentTarget.value)}
				disabled={!mutationAllowed || installRunning}
			>
				{#each devices as option (option)}
					<option value={option}>{option}</option>
				{/each}
			</select>
		</div>
		<button
			type="button"
			class="btn btn-sm"
			disabled={!mutationAllowed || installRunning}
			onclick={() => oninstall(engine.id, device)}
		>
			{#if installRunning}
				<LoaderCircle class="size-3.5 animate-spin" aria-hidden="true" />
				Installing…
			{:else}
				{engine.installed ? 'Reinstall' : 'Install'}
			{/if}
		</button>
		<p class="min-w-0 flex-1 text-[11px] leading-4 text-ink-muted">
			{#if mutationAllowed}
				Runs <code class="metric">uv sync</code> for a device variant and records an audit line. The
				API installs verified variants only; an experimental one is a terminal decision (<code
					class="metric">--experimental</code
				>).
			{:else}
				Engine mutation is off. Run <code class="metric">{cliHint}</code> in a terminal, then reload.
			{/if}
		</p>
	</div>

	{#if installJob}
		<div class="border-b border-rule px-3 py-2">
			<div class="flex items-center gap-2">
				<span
					class={`chip ${installJob.status === 'failed' ? 'chip-danger' : installJob.status === 'succeeded' ? 'chip-success' : 'chip-signal'}`}
				>
					{installJob.status}
				</span>
				<span class="min-w-0 flex-1 truncate text-[11px] text-ink-secondary">
					{installJob.message || 'install'}
				</span>
				<span class="metric text-[11px] text-ink-muted">
					{Math.round(installJob.progress * 100)}%
				</span>
			</div>
			<div class="progress-track mt-1">
				<div class="progress-fill" style={`width: ${Math.round(installJob.progress * 100)}%`}></div>
			</div>
			{#if installJob.error}
				<p class="mt-1 text-[11px] text-danger">{installJob.error.message}</p>
			{/if}
		</div>
	{/if}

	<ul class="divide-y divide-rule">
		{#each modelRows as row (row.model.alias)}
			<li class="px-3 py-2">
				<div class="flex flex-wrap items-center gap-2">
					<div class="min-w-0 flex-1">
						<p class="metric truncate text-[12px] text-ink">{row.model.alias}</p>
						<p class="text-[11px] text-ink-muted">
							{MODE_LABELS[row.model.mode]} · {row.model.qualityTier} · {formatVram(
								row.model.estVramGb * 1024
							)} est. VRAM{#if row.model.supportsInstruct}
								· instruct{/if}
						</p>
					</div>
					<div class="flex shrink-0 items-center gap-1">
						{#if row.model.loaded}
							<span class="chip chip-success">Loaded</span>
						{/if}
						{#if row.model.downloaded}
							<span class="chip">
								<Check class="size-3" aria-hidden="true" />
								Weights present
							</span>
						{:else}
							<button
								type="button"
								class="btn btn-sm"
								disabled={!mutationAllowed || row.running}
								onclick={() => ondownload(engine.id, row.model)}
							>
								{#if row.running}
									<LoaderCircle class="size-3.5 animate-spin" aria-hidden="true" />
									{Math.round((row.job?.progress ?? 0) * 100)}%
								{:else}
									<Download class="size-3.5" aria-hidden="true" />
									Download
								{/if}
							</button>
						{/if}
					</div>
				</div>
				<p class="metric mt-0.5 truncate text-[10px] text-ink-muted" title={row.model.hfRepo}>
					{row.model.hfRepo || 'no repository declared'}
				</p>
				{#if row.job?.error}
					<p class="mt-1 text-[11px] text-danger">{row.job.error.message}</p>
				{/if}
			</li>
		{/each}
		{#if engine.models.length === 0}
			<li class="px-3 py-2 text-[11px] text-ink-muted">No models declared in this manifest.</li>
		{/if}
	</ul>
</section>
