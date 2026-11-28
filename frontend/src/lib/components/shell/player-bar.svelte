<script lang="ts">
	import Download from '@lucide/svelte/icons/download';
	import LoaderCircle from '@lucide/svelte/icons/loader-circle';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import Volume2 from '@lucide/svelte/icons/volume-2';
	import X from '@lucide/svelte/icons/x';
	import Waveform from '#lib/components/common/waveform.svelte';
	import { formatClock } from '#lib/format.js';
	import { getPlayer } from '#lib/stores/player.svelte.js';

	const player = getPlayer();
	const rates = ['0.75', '1', '1.25', '1.5', '2'];
</script>

{#if player.clip}
	<section
		data-testid="player-bar"
		class="flex h-24 shrink-0 items-center gap-3 border-t border-rule bg-panel px-3 md:px-4"
		aria-label="Player"
	>
		<button
			type="button"
			class="flex size-10 shrink-0 items-center justify-center rounded-control bg-signal text-on-signal transition-colors hover:bg-signal-strong disabled:opacity-60"
			aria-label={player.playing ? 'Pause audio' : 'Play audio'}
			onclick={() => player.toggle()}
		>
			{#if player.loading}
				<LoaderCircle class="size-4 animate-spin" aria-hidden="true" />
			{:else if player.playing}
				<Pause class="size-4" aria-hidden="true" />
			{:else}
				<Play class="size-4" aria-hidden="true" />
			{/if}
		</button>

		<div class="w-[132px] min-w-0 shrink-0 md:w-[188px]">
			<p data-testid="player-title" class="truncate text-[12px] font-medium text-ink">
				{player.clip.title}
			</p>
			<p class="truncate text-[11px] text-ink-muted">{player.clip.subtitle}</p>
			<p class="metric hidden text-[10px] text-ink-muted md:block">
				{player.clip.cached ? 'cache hit' : 'rendered'} · {player.clip.format}
			</p>
		</div>

		<div class="min-w-0 flex-1">
			<div class="mb-0.5 flex items-center justify-between">
				<span data-testid="player-time" class="metric text-[11px] text-ink-secondary">
					{formatClock(player.currentTime)} / {formatClock(player.duration)}
				</span>
				<span class="metric text-[10px] text-ink-muted">
					{player.clip.engineId} · {player.clip.modelAlias}
				</span>
			</div>
			<Waveform
				peaks={player.peaks}
				progress={player.progress}
				currentTime={player.currentTime}
				duration={player.duration}
				onSeek={(ratio: number) => player.seek(ratio)}
			/>
			{#if player.error}
				<p class="truncate text-[11px] text-danger" aria-live="polite">{player.error}</p>
			{/if}
		</div>

		<div class="flex shrink-0 items-center gap-1">
			<select
				class="select metric hidden h-7 w-[68px] text-[11px] sm:block"
				aria-label="Playback speed"
				value={String(player.rate)}
				onchange={(event) => player.setRate(Number(event.currentTarget.value))}
			>
				{#each rates as rate (rate)}
					<option value={rate}>{rate}×</option>
				{/each}
			</select>
			<div class="hidden items-center gap-1 xl:flex">
				<Volume2 class="size-3.5 text-ink-muted" aria-hidden="true" />
				<input
					class="h-1 w-16 cursor-pointer"
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
				class="btn btn-ghost btn-icon"
				aria-label="Download audio"
				title="Download audio"
				onclick={() => void player.download()}
			>
				<Download class="size-3.5" aria-hidden="true" />
			</button>
			<button
				type="button"
				class="btn btn-ghost btn-icon"
				aria-label="Close player"
				title="Close player"
				onclick={() => player.close()}
			>
				<X class="size-3.5" aria-hidden="true" />
			</button>
		</div>
	</section>
{/if}
