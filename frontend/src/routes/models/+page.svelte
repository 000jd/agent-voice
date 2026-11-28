<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import Boxes from '@lucide/svelte/icons/boxes';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import { describeApiError } from '#lib/api/client.js';
	import { getJob } from '#lib/api/jobs.js';
	import { getSettings } from '#lib/api/settings.js';
	import { downloadModel, installEngine } from '#lib/api/studio.js';
	import type { ApiErrorPayload, Job, ModelSummary } from '#lib/api/types.js';
	import ConfirmDialog from '#lib/components/common/confirm-dialog.svelte';
	import EmptyState from '#lib/components/common/empty-state.svelte';
	import ErrorNotice from '#lib/components/common/error-notice.svelte';
	import EnginePanel from '#lib/components/models/engine-panel.svelte';
	import { events, jobFromEvent } from '#lib/stores/events.svelte.js';
	import { studio } from '#lib/stores/studio.svelte.js';
	import { toasts } from '#lib/stores/toasts.svelte.js';

	let mutationAllowed = $state(false);
	let settingsLoading = $state(true);
	let settingsError = $state<ApiErrorPayload | null>(null);

	let jobs = $state<Record<string, Job>>({});
	const jobKeys: Record<string, string> = {};

	let unloadOpen = $state(false);
	let downloadOpen = $state(false);
	let downloadEngineId = $state('');
	let downloadModelAlias = $state('');

	let unsubscribe: (() => void) | null = null;
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	const tts = $derived(studio.ttsEngines);
	const asr = $derived(studio.asrEngines);

	onMount(() => {
		if (studio.engines.length === 0) void studio.refreshCatalog();
		void loadMutationSetting();
		unsubscribe = events.subscribe((message) => {
			const job = jobFromEvent(message);
			if (job) applyJob(job);
		});
		pollTimer = setInterval(() => {
			for (const jobId of Object.keys(jobKeys)) void pollJob(jobId);
		}, 2000);
	});

	onDestroy(() => {
		unsubscribe?.();
		if (pollTimer !== null) clearInterval(pollTimer);
		pollTimer = null;
	});

	async function loadMutationSetting(): Promise<void> {
		settingsLoading = true;
		settingsError = null;
		try {
			const snapshot = await getSettings();
			const entry = snapshot.settings.find((item) => item.key === 'ALLOW_AGENT_ENGINE_MUTATION');
			mutationAllowed = entry?.value === true;
		} catch (error) {
			settingsError = describeApiError(error);
			mutationAllowed = false;
		} finally {
			settingsLoading = false;
		}
	}

	async function pollJob(jobId: string): Promise<void> {
		try {
			applyJob(await getJob(jobId));
		} catch {
			// The next tick retries; a transient poll failure is not fatal.
		}
	}

	function applyJob(job: Job): void {
		const key = jobKeys[job.jobId];
		if (!key) return;
		jobs = { ...jobs, [key]: job };
		if (job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled') {
			delete jobKeys[job.jobId];
			void studio.refreshCatalog();
			if (job.status === 'succeeded') toasts.success('Job finished', job.message || job.kind);
			if (job.status === 'failed') {
				toasts.error('Job failed', job.error?.message ?? job.message ?? job.kind);
			}
		}
	}

	function track(jobId: string, key: string): void {
		jobKeys[jobId] = key;
		void pollJob(jobId);
	}

	async function startInstall(engineId: string, device: string): Promise<void> {
		try {
			const started = await installEngine(engineId, device);
			track(started.jobId, `install:${engineId}`);
			toasts.info('Install queued', `${engineId} · ${device}`);
		} catch (error) {
			toasts.error('Install failed', describeApiError(error).message);
		}
	}

	function handleInstall(engineId: string, device: string): void {
		void startInstall(engineId, device);
	}

	function handleDownload(engineId: string, model: ModelSummary): void {
		askDownload(engineId, model);
	}

	function askDownload(engineId: string, model: ModelSummary): void {
		downloadEngineId = engineId;
		downloadModelAlias = model.alias;
		downloadOpen = true;
	}

	async function startDownload(): Promise<void> {
		const engineId = downloadEngineId;
		const alias = downloadModelAlias;
		if (!engineId || !alias) return;
		try {
			const started = await downloadModel(engineId, alias);
			track(started.jobId, `download:${engineId}:${alias}`);
			toasts.info('Weight download queued', `${engineId} · ${alias}`);
		} catch (error) {
			toasts.error('Download failed', describeApiError(error).message);
		}
	}

	async function unloadAll(): Promise<void> {
		try {
			const result = await studio.unloadAll();
			toasts.success(
				'Models unloaded',
				`${result.unloaded.length} engines · ${result.cancelledJobs} jobs cancelled`
			);
		} catch (error) {
			toasts.error('Unload failed', describeApiError(error).message);
		}
	}
</script>

{#snippet boxesIcon()}
	<Boxes class="size-5" aria-hidden="true" />
{/snippet}

<div class="h-full overflow-y-auto">
	<div class="mx-auto w-full max-w-[1100px] px-4 py-4 md:px-6">
		<div class="flex flex-wrap items-center gap-2">
			<p class="text-[12px] text-ink-secondary">
				{tts.length} text-to-speech · {asr.length} recognition · manifests under the engines directory
			</p>
			<div class="ml-auto flex items-center gap-2">
				<button
					type="button"
					class="btn btn-sm btn-ghost"
					onclick={() => {
						void studio.refreshCatalog();
						void loadMutationSetting();
					}}
				>
					<RefreshCw class="size-3.5" aria-hidden="true" />
					Refresh
				</button>
				<button
					type="button"
					class="btn btn-sm"
					disabled={studio.unloading}
					onclick={() => (unloadOpen = true)}
				>
					{studio.unloading ? 'Unloading…' : 'Unload all'}
				</button>
			</div>
		</div>

		{#if settingsError}
			<div class="mt-3">
				<ErrorNotice
					error={settingsError}
					onRetry={() => void loadMutationSetting()}
					retryLabel="Retry"
				/>
			</div>
		{/if}

		{#if !settingsLoading && !mutationAllowed && !settingsError}
			<div class="mt-3 rounded-panel border border-rule bg-surface px-3 py-2">
				<p class="text-[12px] leading-5 text-ink-secondary">
					<strong class="font-medium text-ink">Engine mutation is off.</strong>
					Install and download actions stay disabled. Run
					<code class="metric"
						>agent-voice engines install &lt;engine&gt; --device &lt;device&gt;</code
					>
					in a terminal, or set <code class="metric">ALLOW_AGENT_ENGINE_MUTATION=true</code> in the daemon's
					config and restart.
				</p>
			</div>
		{/if}

		{#if studio.catalogLoading && studio.engines.length === 0}
			<div class="mt-4 grid gap-3 lg:grid-cols-2">
				{#each Array.from({ length: 2 }, (_, index) => index) as index (index)}
					<div class="card h-48 p-3">
						<div class="skeleton h-4 w-1/2"></div>
						<div class="skeleton mt-3 h-8 w-full"></div>
						<div class="skeleton mt-6 h-6 w-full"></div>
					</div>
				{/each}
			</div>
		{:else if studio.engines.length === 0}
			<div class="mt-4">
				<EmptyState
					title="No engine manifests found"
					description="The daemon reads engines from its engines directory. Install the packaged engines with `agent-voice setup`, then reload."
					icon={boxesIcon}
				>
					<button type="button" class="btn btn-sm" onclick={() => void studio.refreshCatalog()}>
						Reload manifests
					</button>
				</EmptyState>
			</div>
		{:else}
			<section class="mt-4" aria-labelledby="models-tts">
				<h2 id="models-tts" class="mb-2 text-[13px] font-medium text-ink">
					Text to speech <span class="metric text-[11px] text-ink-muted">{tts.length}</span>
				</h2>
				<div class="grid items-start gap-3 xl:grid-cols-2">
					{#each tts as engine (engine.id)}
						<EnginePanel
							{engine}
							{mutationAllowed}
							{jobs}
							oninstall={handleInstall}
							ondownload={handleDownload}
						/>
					{/each}
				</div>
				{#if tts.length === 0}
					<p class="text-[12px] text-ink-secondary">No text-to-speech engine is installed.</p>
				{/if}
			</section>

			<section class="mt-6" aria-labelledby="models-asr">
				<h2 id="models-asr" class="mb-2 text-[13px] font-medium text-ink">
					Speech recognition <span class="metric text-[11px] text-ink-muted">{asr.length}</span>
				</h2>
				<div class="grid items-start gap-3 xl:grid-cols-2">
					{#each asr as engine (engine.id)}
						<EnginePanel
							{engine}
							{mutationAllowed}
							{jobs}
							oninstall={handleInstall}
							ondownload={handleDownload}
						/>
					{/each}
				</div>
				{#if asr.length === 0}
					<p class="text-[12px] text-ink-secondary">
						No recognition engine is installed, so Transcribe has nothing to call.
					</p>
				{/if}
			</section>

			<section class="mt-6 border-t border-rule pt-3">
				<h2 class="text-[12px] font-medium text-ink-secondary">Licences and downloads</h2>
				<p class="mt-1 max-w-[80ch] text-[11px] leading-4 text-ink-muted">
					The daemon does not expose a licence field, so this screen never invents one. Each model
					row shows the exact repository it would fetch from; check that model card for its terms
					before downloading. A weight download only starts after you confirm it here, and the
					daemon writes an audit line for every install or download.
				</p>
			</section>
		{/if}
	</div>
</div>

<ConfirmDialog
	bind:open={unloadOpen}
	title="Unload all models"
	message="Every loaded engine is unloaded and pending jobs are cancelled."
	detail="VRAM returns to the driver baseline. The next render reloads the model from disk, which can take a few seconds."
	confirmLabel="Unload all"
	danger
	onconfirm={() => void unloadAll()}
/>

<ConfirmDialog
	bind:open={downloadOpen}
	title="Download model weights"
	message={`Download ${downloadEngineId} · ${downloadModelAlias}?`}
	detail="The daemon fetches the declared repository from huggingface.co with the local huggingface client and keeps it in the on-disk model cache. Nothing else on this screen reaches the network."
	confirmLabel="Download weights"
	onconfirm={() => void startDownload()}
/>
