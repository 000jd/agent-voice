<script lang="ts">
	import { onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import ArrowUpRight from '@lucide/svelte/icons/arrow-up-right';
	import FileMusic from '@lucide/svelte/icons/file-music';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import Sparkles from '@lucide/svelte/icons/sparkles';
	import BrandMark from '#lib/components/brand-mark.svelte';
	import ClipRow from '#lib/components/studio/clip-row.svelte';
	import ErrorNotice from '#lib/components/studio/error-notice.svelte';
	import { Button } from '#lib/components/ui/button/index.js';
	import { formatGpuLabel, formatVram } from '#lib/format.js';
	import { studio } from '#lib/stores/studio.svelte.js';

	const recent = $derived(studio.sessionClips.slice(0, 5));
	const loadedModels = $derived(studio.gpu?.loadedModels.length ?? 0);

	onMount(() => {
		if (studio.engines.length === 0) void studio.refreshCatalog();
		void studio.refreshConnection();
		void studio.refreshGpu();
	});

	function refresh(): void {
		void studio.refreshConnection();
		void studio.refreshGpu();
		void studio.refreshCatalog();
	}
</script>

<div class="mb-5 flex items-end justify-between gap-4">
	<div>
		<p class="text-[11px] text-text-muted">Local workspace</p>
		<h1 class="mt-1 text-[26px] leading-8 font-semibold tracking-[-0.025em]">Studio</h1>
		<p class="mt-1 text-[12px] leading-5 text-text-secondary">
			A quiet room for speech. Start with a line, a voice, or a transcript.
		</p>
	</div>
	<button
		type="button"
		class="hidden size-8 items-center justify-center rounded-lg text-text-muted hover:bg-elevated-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/30 focus-visible:outline-none sm:flex"
		aria-label="Refresh studio state"
		title="Refresh studio state"
		onclick={refresh}
	>
		<RefreshCw class="size-4" aria-hidden="true" />
	</button>
</div>

{#if studio.connection === 'offline' || (studio.catalogError && studio.engines.length === 0)}
	<div class="mb-4">
		<ErrorNotice
			error={studio.catalogError ??
				'The daemon is not reachable. Start `agent-voice serve`, then refresh.'}
			onRetry={refresh}
		/>
	</div>
{/if}

<div class="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
	<section
		class="studio-texture relative min-h-[300px] overflow-hidden rounded-2xl border border-border bg-elevated p-6 md:p-8"
	>
		<BrandMark
			class="pointer-events-none absolute -right-4 -bottom-10 size-56 text-white/[0.025]"
		/>
		<div class="relative z-10 max-w-lg">
			<p class="text-[11px] text-text-muted">Create</p>
			<h2 class="mt-2 text-[30px] leading-[1.12] font-semibold tracking-[-0.03em]">
				Give a line<br />a voice.
			</h2>
			<p class="mt-3 max-w-md text-[13px] leading-6 text-text-secondary">
				Compose a take, choose a cloned or preset timbre, then audition it in the docked player.
			</p>
			<div class="mt-7 flex flex-wrap items-center gap-5">
				<Button href={resolve('speak')} class="h-10 rounded-xl px-5">
					Open studio
					<ArrowUpRight class="size-4" aria-hidden="true" />
				</Button>
				<a
					href={`${resolve('voices')}?design=1`}
					class="inline-flex items-center gap-1.5 text-[12px] text-text-secondary transition-colors hover:text-foreground"
				>
					<Sparkles class="size-3.5" aria-hidden="true" />
					Design a voice
				</a>
				<a
					href={resolve('transcribe')}
					class="inline-flex items-center gap-1.5 text-[12px] text-text-secondary transition-colors hover:text-foreground"
				>
					<FileMusic class="size-3.5" aria-hidden="true" />
					Transcribe
				</a>
			</div>
		</div>
	</section>

	<aside class="rounded-xl border border-border bg-panel p-4">
		<div class="flex items-center justify-between gap-2">
			<h2 class="text-[11px] font-medium text-text-muted">System</h2>
			<span class="flex items-center gap-1.5 text-[10px] text-text-secondary">
				<span
					class={`size-1.5 rounded-full ${
						studio.connection === 'connected'
							? 'bg-success'
							: studio.connection === 'offline'
								? 'bg-danger'
								: 'bg-text-muted'
					}`}
				></span>
				{studio.connection === 'connected'
					? 'Connected'
					: studio.connection === 'offline'
						? 'Offline'
						: 'Checking'}
			</span>
		</div>
		<dl class="mt-3 divide-y divide-border font-mono text-[10px] tabular-nums">
			<div class="flex items-center justify-between gap-3 py-2">
				<dt class="text-text-muted">Version</dt>
				<dd>{studio.health?.version ?? '—'} / p{studio.health?.protocolVersion ?? '—'}</dd>
			</div>
			<div class="flex items-center justify-between gap-3 py-2">
				<dt class="text-text-muted">GPU</dt>
				<dd class="text-right">{formatGpuLabel(studio.gpu)}</dd>
			</div>
			<div class="flex items-center justify-between gap-3 py-2">
				<dt class="text-text-muted">Loaded</dt>
				<dd>{loadedModels} models</dd>
			</div>
			<div class="flex items-center justify-between gap-3 py-2">
				<dt class="text-text-muted">Queue</dt>
				<dd>{studio.gpu?.queueDepth ?? 0} / {studio.gpu?.inFlight ?? 0} active</dd>
			</div>
			{#if studio.gpu?.vramFreeMb !== null && studio.gpu?.vramFreeMb !== undefined}
				<div class="flex items-center justify-between gap-3 py-2">
					<dt class="text-text-muted">VRAM free</dt>
					<dd>{formatVram(studio.gpu.vramFreeMb)}</dd>
				</div>
			{/if}
		</dl>
		<a
			href={resolve('models')}
			class="mt-3 inline-flex items-center gap-1 text-[11px] text-text-secondary transition-colors hover:text-foreground"
		>
			Inspect engines
			<ArrowUpRight class="size-3" aria-hidden="true" />
		</a>
	</aside>
</div>

<section class="mt-6">
	<div class="mb-2 flex items-center justify-between gap-2">
		<h2 class="text-[11px] font-medium text-text-muted">Recent renders</h2>
		<a
			href={resolve('history')}
			class="inline-flex items-center gap-1 text-[11px] text-text-secondary hover:text-foreground"
		>
			Session history
			<ArrowUpRight class="size-3" aria-hidden="true" />
		</a>
	</div>
	{#if recent.length === 0}
		<div class="rounded-xl border border-border bg-panel px-4 py-8 text-center">
			<p class="text-[12px] text-text-secondary">No renders in this session yet.</p>
			<a href={resolve('speak')} class="mt-1 inline-block text-[11px] text-primary hover:underline">
				Start with a line
			</a>
		</div>
	{:else}
		<div class="rounded-xl border border-border bg-panel px-3">
			<div class="divide-y divide-border">
				{#each recent as clip (clip.id)}
					<ClipRow {clip} />
				{/each}
			</div>
		</div>
	{/if}
</section>
