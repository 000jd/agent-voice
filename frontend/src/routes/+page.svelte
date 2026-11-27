<script lang="ts">
	import { onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import ArrowUpRight from '@lucide/svelte/icons/arrow-up-right';
	import AudioLines from '@lucide/svelte/icons/audio-lines';
	import FileMusic from '@lucide/svelte/icons/file-music';
	import History from '@lucide/svelte/icons/history';
	import RefreshCw from '@lucide/svelte/icons/refresh-cw';
	import Sparkles from '@lucide/svelte/icons/sparkles';
	import PageHeader from '#lib/components/page-header.svelte';
	import ClipRow from '#lib/components/studio/clip-row.svelte';
	import ErrorNotice from '#lib/components/studio/error-notice.svelte';
	import { Button } from '#lib/components/ui/button/index.js';
	import { formatGpuLabel, formatVram } from '#lib/format.js';
	import { studio } from '#lib/stores/studio.svelte.js';

	const quickLinks = [
		{
			href: resolve('speak'),
			title: 'Speak',
			body: 'Compose a line and generate it with a cloned, preset or designed voice.',
			icon: AudioLines
		},
		{
			href: `${resolve('voices')}?design=1`,
			title: 'Design a voice',
			body: 'Describe a timbre, audition takes and save the one you want to reuse.',
			icon: Sparkles
		},
		{
			href: resolve('transcribe'),
			title: 'Transcribe',
			body: 'Upload a clip, get the text, timed segments and subtitle files.',
			icon: FileMusic
		}
	];

	const recent = $derived(studio.sessionClips.slice(0, 5));
	const loadedModels = $derived(studio.gpu?.loadedModels.length ?? 0);

	onMount(() => {
		if (studio.engines.length === 0) void studio.refreshCatalog();
		void studio.refreshConnection();
		void studio.refreshGpu();
	});
</script>

<PageHeader
	title="Dashboard"
	description="A quiet home for the local daemon. Start a render, design a voice, or inspect the live engine state."
>
	<Button
		variant="outline"
		size="sm"
		onclick={() => {
			void studio.refreshConnection();
			void studio.refreshGpu();
			void studio.refreshCatalog();
		}}
	>
		<RefreshCw class="size-3.5" aria-hidden="true" />
		Refresh
	</Button>
</PageHeader>

{#if studio.connection === 'offline' || (studio.catalogError && studio.engines.length === 0)}
	<div class="mb-4">
		<ErrorNotice
			error={studio.catalogError ??
				'The daemon is not reachable. Start `agent-voice serve`, then refresh.'}
			onRetry={() => {
				void studio.refreshConnection();
				void studio.refreshGpu();
				void studio.refreshCatalog();
			}}
		/>
	</div>
{/if}

<div
	class="flex flex-wrap items-center gap-x-5 gap-y-1.5 rounded-xl border border-border bg-elevated px-3.5 py-2.5 font-mono text-[10px] text-text-secondary tabular-nums"
>
	<span class="flex items-center gap-1.5">
		<span
			class={`size-1.5 rounded-full ${studio.connection === 'connected' ? 'bg-success' : studio.connection === 'offline' ? 'bg-danger' : 'bg-text-muted'}`}
		></span>
		{studio.connection === 'connected'
			? 'daemon connected'
			: studio.connection === 'offline'
				? 'daemon offline'
				: 'checking daemon'}
	</span>
	<span>version {studio.health?.version ?? '—'}</span>
	<span>protocol {studio.health?.protocolVersion ?? '—'}</span>
	<span>gpu {formatGpuLabel(studio.gpu)}</span>
	<span>{loadedModels} loaded</span>
	<span>queue {studio.gpu?.queueDepth ?? 0} · in flight {studio.gpu?.inFlight ?? 0}</span>
	{#if studio.gpu?.activeEngine}
		<span class="text-primary">{studio.gpu.activeEngine}</span>
	{/if}
	{#if studio.gpu?.vramFreeMb !== null && studio.gpu?.vramFreeMb !== undefined}
		<span>{formatVram(studio.gpu.vramFreeMb)} free</span>
	{/if}
</div>

<div class="mt-5 grid gap-3 md:grid-cols-3">
	{#each quickLinks as link (link.title)}
		<a
			href={link.href}
			class="group flex min-h-[128px] flex-col justify-between rounded-xl border border-border bg-elevated p-4 transition-colors hover:border-primary/30 hover:bg-elevated-hover/60 focus-visible:ring-3 focus-visible:ring-ring/30 focus-visible:outline-none"
		>
			<div class="flex items-start justify-between gap-2">
				<span
					class="flex size-8 items-center justify-center rounded-lg bg-panel text-text-secondary"
				>
					<link.icon class="size-4" aria-hidden="true" />
				</span>
				<ArrowUpRight
					class="size-3.5 text-text-muted transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5 group-hover:text-primary"
					aria-hidden="true"
				/>
			</div>
			<div class="mt-4">
				<h2 class="text-[13px] font-medium text-foreground">{link.title}</h2>
				<p class="mt-1 text-[11px] leading-4 text-text-secondary">{link.body}</p>
			</div>
		</a>
	{/each}
</div>

<section class="mt-7">
	<div class="mb-2 flex items-center justify-between gap-2">
		<h2 class="text-[10px] font-medium tracking-[0.08em] text-text-muted uppercase">
			Recent session renders
		</h2>
		<a
			href={resolve('history')}
			class="inline-flex items-center gap-1 text-[10px] text-text-secondary hover:text-foreground"
		>
			<History class="size-3" aria-hidden="true" />
			History
		</a>
	</div>
	{#if recent.length === 0}
		<div class="rounded-xl border border-border bg-elevated px-4 py-7 text-center">
			<p class="text-[12px] text-text-secondary">No renders in this session yet.</p>
			<a href={resolve('speak')} class="mt-1 inline-block text-[11px] text-primary hover:underline">
				Start with Speak
			</a>
		</div>
	{:else}
		<div class="rounded-xl border border-border bg-elevated px-3">
			<div class="divide-y divide-border">
				{#each recent as clip (clip.id)}
					<ClipRow {clip} />
				{/each}
			</div>
		</div>
	{/if}
</section>
