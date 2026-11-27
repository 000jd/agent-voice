<script lang="ts">
	import { onMount } from 'svelte';
	import AudioLines from '@lucide/svelte/icons/audio-lines';
	import CircleCheck from '@lucide/svelte/icons/circle-check';
	import Cpu from '@lucide/svelte/icons/cpu';
	import Info from '@lucide/svelte/icons/info';
	import Mic from '@lucide/svelte/icons/mic';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import Server from '@lucide/svelte/icons/server';
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
	import { loadDashboard, type DashboardData } from '#lib/api/dashboard.js';
	import PageHeader from '#lib/components/page-header.svelte';
	import { Button } from '#lib/components/ui/button/index.js';
	import * as Card from '#lib/components/ui/card/index.js';
	import * as Tooltip from '#lib/components/ui/tooltip/index.js';
	import { cn } from '#lib/utils.js';

	type ViewState =
		| { kind: 'loading' }
		| { kind: 'error'; message: string }
		| { kind: 'ready'; data: DashboardData };

	let view = $state<ViewState>({ kind: 'loading' });
	let refreshing = $state(false);

	const engines = $derived(view.kind === 'ready' ? view.data.engines : []);
	const ttsCount = $derived(engines.filter((engine) => engine.kind === 'tts').length);
	const asrCount = $derived(engines.filter((engine) => engine.kind === 'asr').length);
	const installedCount = $derived(engines.filter((engine) => engine.installed).length);
	const modelCount = $derived(engines.reduce((sum, engine) => sum + engine.models.length, 0));
	const loadedModelCount = $derived(
		engines.reduce((sum, engine) => sum + engine.models.filter((model) => model.loaded).length, 0)
	);

	async function refresh(): Promise<void> {
		const hadData = view.kind === 'ready';
		if (hadData) {
			refreshing = true;
		} else {
			view = { kind: 'loading' };
		}
		try {
			view = { kind: 'ready', data: await loadDashboard() };
		} catch (error) {
			view = {
				kind: 'error',
				message: error instanceof Error ? error.message : 'Unknown error'
			};
		} finally {
			refreshing = false;
		}
	}

	onMount(() => {
		void refresh();
	});
</script>

<PageHeader
	title="Dashboard"
	description="Live state of the local daemon. This page reads only /health and /v1/engines; nothing leaves this machine."
>
	<Button variant="outline" size="sm" onclick={() => void refresh()} disabled={refreshing}>
		<RefreshCw class={refreshing ? 'size-4 animate-spin' : 'size-4'} aria-hidden="true" />
		{refreshing ? 'Refreshing' : 'Refresh'}
	</Button>
</PageHeader>

{#if view.kind === 'loading'}
	<div
		class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3"
		aria-busy="true"
		aria-label="Loading dashboard"
	>
		{#each [0, 1, 2] as card (card)}
			<Card.Root>
				<Card.Header>
					<div class="h-4 w-28 animate-pulse rounded bg-muted"></div>
					<div class="mt-2 h-8 w-20 animate-pulse rounded bg-muted"></div>
				</Card.Header>
				<Card.Content>
					<div class="h-3 w-full animate-pulse rounded bg-muted"></div>
					<div class="mt-2 h-3 w-2/3 animate-pulse rounded bg-muted"></div>
				</Card.Content>
			</Card.Root>
		{/each}
	</div>
{:else if view.kind === 'error'}
	<Card.Root class="border-destructive/40">
		<Card.Header>
			<Card.Title class="flex items-center gap-2 text-destructive">
				<TriangleAlert class="size-5" aria-hidden="true" />
				Daemon not reachable
			</Card.Title>
			<Card.Description>{view.message}</Card.Description>
		</Card.Header>
		<Card.Content class="space-y-3 text-sm">
			<p class="text-muted-foreground">Start the daemon in another terminal, then refresh:</p>
			<pre class="overflow-x-auto rounded-lg border bg-muted/50 px-3 py-2 text-xs"><code
					>agent-voice serve</code
				></pre>
			<p class="text-muted-foreground">
				If <code class="rounded bg-muted px-1 py-0.5 text-xs">AGENT_VOICE_API_KEY</code> is set, put
				the same value in <code class="rounded bg-muted px-1 py-0.5 text-xs">frontend/.env</code> as
				<code class="rounded bg-muted px-1 py-0.5 text-xs">AGENT_VOICE_DEV_API_KEY</code> and restart
				Vite. The key is never exposed as a public variable.
			</p>
		</Card.Content>
	</Card.Root>
{:else}
	<div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
		<Card.Root>
			<Card.Header class="flex flex-row items-start justify-between gap-3">
				<div>
					<Card.Description>Daemon</Card.Description>
					<Card.Title class="mt-1 flex items-center gap-2 text-xl">
						<Server class="size-5 text-muted-foreground" aria-hidden="true" />
						{view.data.health.version}
					</Card.Title>
				</div>
				<span
					class="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 px-2 py-1 text-xs font-medium text-emerald-700 dark:text-emerald-400"
				>
					<CircleCheck class="size-3.5" aria-hidden="true" />
					{view.data.health.status}
				</span>
			</Card.Header>
			<Card.Content class="text-sm text-muted-foreground">
				Protocol v{view.data.health.protocolVersion}
			</Card.Content>
		</Card.Root>

		<Card.Root>
			<Card.Header>
				<Card.Description>Engines</Card.Description>
				<Card.Title class="mt-1 text-xl">{engines.length}</Card.Title>
			</Card.Header>
			<Card.Content class="flex flex-wrap gap-2 text-xs">
				<span class="rounded-full bg-muted px-2 py-1">{ttsCount} TTS</span>
				<span class="rounded-full bg-muted px-2 py-1">{asrCount} ASR</span>
				<span class="rounded-full bg-muted px-2 py-1">{installedCount} installed</span>
			</Card.Content>
		</Card.Root>

		<Card.Root>
			<Card.Header class="flex flex-row items-start justify-between gap-3">
				<div>
					<Card.Description>Models</Card.Description>
					<Card.Title class="mt-1 text-xl">{modelCount}</Card.Title>
				</div>
				<Cpu class="size-5 text-muted-foreground" aria-hidden="true" />
			</Card.Header>
			<Card.Content class="text-sm text-muted-foreground">
				{loadedModelCount} loaded · GPU {view.data.health.torchLoaded ? 'active' : 'idle'}
			</Card.Content>
		</Card.Root>
	</div>

	<section class="mt-8">
		<div class="mb-3 flex items-center gap-2">
			<h2 class="text-lg font-semibold">Engines</h2>
			<Tooltip.Provider>
				<Tooltip.Root>
					<Tooltip.Trigger
						class="inline-flex size-5 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
						aria-label="About the engine list"
					>
						<Info class="size-3.5" aria-hidden="true" />
					</Tooltip.Trigger>
					<Tooltip.Content>
						Read from <code>/v1/engines</code>. A model is never loaded just by opening this page.
					</Tooltip.Content>
				</Tooltip.Root>
			</Tooltip.Provider>
		</div>

		{#if engines.length === 0}
			<Card.Root>
				<Card.Content class="py-8 text-center text-sm text-muted-foreground">
					The daemon reports no engines. Check <code>engines/</code> and its logs.
				</Card.Content>
			</Card.Root>
		{:else}
			<div class="space-y-3">
				{#each engines as engine (engine.id)}
					<Card.Root size="sm">
						<Card.Content class="space-y-3">
							<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
								<div class="flex min-w-0 items-start gap-3">
									<span
										class="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground"
									>
										{#if engine.kind === 'tts'}
											<AudioLines class="size-4" aria-hidden="true" />
										{:else}
											<Mic class="size-4" aria-hidden="true" />
										{/if}
									</span>
									<div class="min-w-0">
										<p class="truncate text-sm font-medium">{engine.name}</p>
										<p class="truncate text-xs text-muted-foreground">
											{engine.id} · {engine.backend}
											{#if engine.backendVersion}
												{engine.backendVersion}
											{/if}
										</p>
									</div>
								</div>
								<div class="flex flex-wrap gap-1.5 text-xs">
									<span class="rounded-full bg-muted px-2 py-1 uppercase">{engine.kind}</span>
									<span
										class={cn(
											'rounded-full px-2 py-1',
											engine.installed
												? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
												: 'bg-amber-500/10 text-amber-700 dark:text-amber-400'
										)}
									>
										{engine.installed ? 'Installed' : 'Not installed'}
									</span>
									<span class="rounded-full bg-muted px-2 py-1">
										{engine.resolvedDevice ?? engine.device}
									</span>
								</div>
							</div>

							{#if engine.whyUnavailable}
								<p class="text-xs text-amber-700 dark:text-amber-400">{engine.whyUnavailable}</p>
							{/if}

							{#if engine.models.length > 0}
								<div class="flex flex-wrap gap-1.5 border-t pt-3 text-xs">
									{#each engine.models as model (model.alias)}
										<span
											class={cn(
												'rounded-md border px-2 py-1',
												model.loaded ? 'border-emerald-500/30 bg-emerald-500/10' : 'bg-background'
											)}
										>
											{model.alias}
											{#if model.loaded}
												<span class="ml-1 text-emerald-700 dark:text-emerald-400">loaded</span>
											{/if}
										</span>
									{/each}
								</div>
							{/if}
						</Card.Content>
					</Card.Root>
				{/each}
			</div>
		{/if}
	</section>
{/if}
