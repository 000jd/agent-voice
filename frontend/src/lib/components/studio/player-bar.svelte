<script lang="ts">
	import { fly } from 'svelte/transition';
	import Download from '@lucide/svelte/icons/download';
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import Volume2 from '@lucide/svelte/icons/volume-2';
	import X from '@lucide/svelte/icons/x';
	import { formatClock } from '#lib/format.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';
	import Waveform from './waveform.svelte';

	let { railCollapsed = false }: { railCollapsed?: boolean } = $props();

	const player = getPlayer();
	const ruler = $derived(
		Array.from({ length: 5 }, (_, index) => formatClock((player.duration * index) / 4))
	);

	function editable(target: EventTarget | null): boolean {
		if (!(target instanceof HTMLElement)) return false;
		return (
			target.tagName === 'INPUT' ||
			target.tagName === 'TEXTAREA' ||
			target.tagName === 'SELECT' ||
			target.isContentEditable
		);
	}

	function onKeyDown(event: KeyboardEvent): void {
		if (event.code !== 'Space' || editable(event.target) || !player.active) return;
		event.preventDefault();
		player.toggle();
	}
</script>

<svelte:window onkeydown={onKeyDown} />

{#if player.clip}
	<div
		data-testid="player-bar"
		class={`fixed right-0 bottom-0 left-0 z-40 border-t border-border bg-panel transition-colors duration-150 ${railCollapsed ? 'md:left-16' : 'md:left-[236px]'}`}
		transition:fly={{ y: 18, duration: 160 }}
	>
		<div class="mx-auto max-w-[1600px] px-4 py-2.5 md:px-6 md:py-3">
			<div class="flex min-w-0 items-center gap-2.5 md:gap-3">
				<button
					type="button"
					class="hover:bg-accent-hover flex size-10 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground transition-colors focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none disabled:opacity-60"
					aria-label={player.playing ? 'Pause audio' : 'Play audio'}
					onclick={() => player.toggle()}
				>
					{#if player.loading}
						<LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
					{:else if player.playing}
						<Pause class="size-4" aria-hidden="true" />
					{:else}
						<Play class="size-4 translate-x-px" aria-hidden="true" />
					{/if}
				</button>
				<div class="min-w-0 flex-1 md:max-w-[320px] md:flex-none">
					<p data-testid="player-title" class="truncate text-[12px] font-medium text-foreground">
						{player.clip.title}
					</p>
					<p class="hidden truncate font-mono text-[10px] text-text-muted md:block">
						{player.clip.engineId} · {player.clip.modelAlias}
					</p>
				</div>
				<div class="ml-auto flex shrink-0 items-center gap-2">
					<span
						data-testid="player-time"
						class="font-mono text-[11px] text-text-secondary tabular-nums"
					>
						{formatClock(player.currentTime)}
						<span class="text-text-muted">/</span>
						{formatClock(player.duration)}
					</span>
					<select
						class="hidden h-7 rounded-md border border-border bg-elevated px-1.5 font-mono text-[10px] text-text-secondary focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30 focus-visible:outline-none sm:block"
						aria-label="Playback speed"
						value={String(player.rate)}
						onchange={(event) => player.setRate(Number(event.currentTarget.value))}
					>
						<option value="0.75">0.75×</option>
						<option value="1">1×</option>
						<option value="1.25">1.25×</option>
						<option value="1.5">1.5×</option>
					</select>
					<div class="hidden items-center gap-1.5 lg:flex">
						<Volume2 class="size-3.5 text-text-muted" aria-hidden="true" />
						<input
							class="h-1 w-16 cursor-pointer accent-primary"
							type="range"
							min="0"
							max="1"
							step="0.05"
							aria-label="Volume"
							value={player.volume}
							oninput={(event) => player.setVolume(Number(event.currentTarget.value))}
						/>
					</div>
					<button
						type="button"
						class="flex size-7 items-center justify-center rounded-md text-text-secondary hover:bg-elevated-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/30 focus-visible:outline-none"
						aria-label="Download audio"
						title="Download audio"
						onclick={() => void player.download()}
					>
						<Download class="size-3.5" aria-hidden="true" />
					</button>
					<button
						type="button"
						class="flex size-7 items-center justify-center rounded-md text-text-muted hover:bg-elevated-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/30 focus-visible:outline-none"
						aria-label="Close player"
						title="Close player"
						onclick={() => player.close()}
					>
						<X class="size-3.5" aria-hidden="true" />
					</button>
				</div>
			</div>

			<div class="mt-2 flex min-w-0 items-center gap-3">
				<div class="hidden w-16 shrink-0 text-[10px] text-text-muted md:block">Take</div>
				<div class="min-w-0 flex-1">
					<div
						class="mb-0.5 flex justify-between font-mono text-[9px] text-text-muted tabular-nums"
					>
						{#each ruler as tick, index (index)}
							<span>{tick}</span>
						{/each}
					</div>
					<Waveform
						peaks={player.peaks}
						progress={player.progress}
						currentTime={player.currentTime}
						duration={player.duration}
						onSeek={(ratio: number) => player.seek(ratio)}
					/>
					{#if player.error}
						<p class="truncate text-[10px] text-danger" aria-live="polite">{player.error}</p>
					{/if}
				</div>
				<div class="hidden w-24 shrink-0 text-right font-mono text-[9px] text-text-muted md:block">
					{player.clip.cached ? 'cached' : 'rendered'}
				</div>
			</div>
		</div>
	</div>
{/if}
