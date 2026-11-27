<script lang="ts">
	import Cpu from '@lucide/svelte/icons/cpu';
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import Power from '@lucide/svelte/icons/power';
	import type { GpuStatus } from '#lib/api/types.js';
	import { formatGpuLabel } from '#lib/format.js';

	let {
		gpu,
		error = '',
		compact = false,
		unloading = false,
		onUnload
	}: {
		gpu: GpuStatus | null;
		error?: string;
		compact?: boolean;
		unloading?: boolean;
		onUnload: () => void;
	} = $props();

	const label = $derived(error ? 'GPU unavailable' : formatGpuLabel(gpu));
	const loaded = $derived(
		Boolean(gpu?.activeEngine || gpu?.loadedModels.length || gpu?.cpuEngines.length)
	);
	const active = $derived(Boolean(gpu?.activeEngine));
	const detail = $derived(
		error ||
			(gpu
				? `${gpu.loadedModels.length} loaded · queue ${gpu.queueDepth} · in flight ${gpu.inFlight}`
				: 'Waiting for the daemon')
	);
</script>

{#if compact}
	<button
		type="button"
		class="flex size-9 items-center justify-center rounded-lg border border-border bg-elevated text-text-secondary hover:bg-elevated-hover hover:text-foreground disabled:cursor-default disabled:hover:bg-elevated"
		title={loaded ? `${detail} — click to unload` : detail}
		aria-label={loaded ? `${label}. Unload all models` : label}
		disabled={!loaded || unloading}
		onclick={() => loaded && onUnload()}
	>
		{#if unloading}
			<LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
		{:else}
			<span class="relative">
				<Cpu class="size-4" aria-hidden="true" />
				<span
					class={`absolute -top-1 -right-1 size-1.5 rounded-full ${active ? 'bg-success' : 'bg-text-muted'}`}
				></span>
			</span>
		{/if}
	</button>
{:else}
	<div
		class="rounded-lg border border-border bg-elevated p-2.5"
		title={detail}
		aria-label={`GPU: ${label}`}
	>
		<div class="flex items-center gap-2">
			<span class="relative flex size-7 items-center justify-center rounded-md bg-panel">
				<Cpu class="size-3.5 text-text-secondary" aria-hidden="true" />
				<span
					class={`absolute top-1 right-1 size-1.5 rounded-full ${active ? 'bg-success' : 'bg-text-muted'}`}
				></span>
			</span>
			<div class="min-w-0 flex-1">
				<p class="truncate text-[11px] font-medium text-foreground">{label}</p>
				<p class="truncate text-[10px] text-text-muted">
					{error ? 'daemon key / connection' : 'GPU state'}
				</p>
			</div>
			<button
				type="button"
				class="flex size-6 items-center justify-center rounded-md text-text-muted hover:bg-elevated-hover hover:text-foreground disabled:opacity-40"
				disabled={!loaded || unloading}
				aria-label="Unload all models"
				title="Unload all models"
				onclick={() => onUnload()}
			>
				{#if unloading}
					<LoaderCircle class="size-3.5 animate-spin" aria-hidden="true" />
				{:else}
					<Power class="size-3.5" aria-hidden="true" />
				{/if}
			</button>
		</div>
	</div>
{/if}
