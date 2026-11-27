<script lang="ts">
	import { clamp, formatClock } from '#lib/format.js';

	let {
		peaks,
		progress,
		currentTime,
		duration,
		onSeek
	}: {
		peaks: number[];
		progress: number;
		currentTime: number;
		duration: number;
		onSeek: (ratio: number) => void;
	} = $props();

	const BAR_COUNT = 96;
	let dragging = $state(false);

	const bars = $derived(
		Array.from({ length: BAR_COUNT }, (_, index) => {
			const value = peaks[index];
			return typeof value === 'number' ? clamp(value, 0.06, 1) : 0;
		})
	);

	function seekFromEvent(event: PointerEvent): void {
		const target = event.currentTarget;
		if (!(target instanceof HTMLElement)) return;
		const rect = target.getBoundingClientRect();
		if (rect.width <= 0) return;
		onSeek(clamp((event.clientX - rect.left) / rect.width, 0, 1));
	}

	function onPointerDown(event: PointerEvent): void {
		dragging = true;
		(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
		seekFromEvent(event);
	}

	function onPointerMove(event: PointerEvent): void {
		if (dragging) seekFromEvent(event);
	}

	function onPointerUp(event: PointerEvent): void {
		dragging = false;
		(event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
	}

	function onKeyDown(event: KeyboardEvent): void {
		if (duration <= 0) return;
		if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
			event.preventDefault();
			const delta = (event.key === 'ArrowLeft' ? -1 : 1) * 5;
			onSeek((currentTime + delta) / duration);
		}
		if (event.key === 'Home') {
			event.preventDefault();
			onSeek(0);
		}
		if (event.key === 'End') {
			event.preventDefault();
			onSeek(1);
		}
	}
</script>

<div
	class="relative h-9 w-full cursor-pointer touch-none rounded-md select-none focus-visible:ring-3 focus-visible:ring-ring/40 focus-visible:outline-none"
	role="slider"
	tabindex="0"
	aria-label="Seek audio"
	aria-valuemin={0}
	aria-valuemax={Math.round(duration * 1000)}
	aria-valuenow={Math.round(currentTime * 1000)}
	aria-valuetext={formatClock(currentTime)}
	onpointerdown={onPointerDown}
	onpointermove={onPointerMove}
	onpointerup={onPointerUp}
	onpointercancel={onPointerUp}
	onkeydown={onKeyDown}
>
	{#if peaks.length === 0}
		<div class="absolute top-1/2 right-0 left-0 h-px bg-wave-unplayed/50"></div>
	{:else}
		<svg class="h-8 w-full" viewBox="0 0 960 40" preserveAspectRatio="none" aria-hidden="true">
			{#each bars as value, index (index)}
				<rect
					x={index * 10 + 1}
					y={20 - Math.max(2, value * 34) / 2}
					width="6"
					height={Math.max(2, value * 34)}
					rx="1"
					class={index / BAR_COUNT <= progress ? 'fill-primary' : 'fill-wave-unplayed/60'}
				/>
			{/each}
		</svg>
	{/if}
	<div
		class="pointer-events-none absolute top-1 bottom-1 w-px bg-primary"
		style={`left: ${clamp(progress, 0, 1) * 100}%`}
	></div>
</div>
