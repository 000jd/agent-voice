<script lang="ts">
	import { clamp, formatClock } from '#lib/format.js';

	let {
		peaks,
		progress,
		currentTime = 0,
		duration = 0,
		onSeek,
		label = 'Seek audio',
		height = 'md'
	}: {
		peaks: number[];
		progress: number;
		currentTime?: number;
		duration?: number;
		onSeek: (ratio: number) => void;
		label?: string;
		height?: 'sm' | 'md';
	} = $props();

	const BARS = 88;
	let dragging = $state(false);

	const bars = $derived(
		Array.from({ length: BARS }, (_, index) => {
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
		if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
			event.preventDefault();
			const step = (event.key === 'ArrowLeft' ? -1 : 1) * 5;
			if (duration > 0) onSeek((currentTime + step) / duration);
			else onSeek(clamp(progress + step / 100, 0, 1));
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
	class={`relative w-full cursor-pointer touch-none select-none ${height === 'sm' ? 'h-6' : 'h-8'}`}
	role="slider"
	tabindex="0"
	aria-label={label}
	aria-valuemin={0}
	aria-valuemax={duration > 0 ? Math.round(duration * 100) : 100}
	aria-valuenow={duration > 0 ? Math.round(currentTime * 100) : Math.round(progress * 100)}
	aria-valuetext={duration > 0 ? formatClock(currentTime) : `${Math.round(progress * 100)}%`}
	onpointerdown={onPointerDown}
	onpointermove={onPointerMove}
	onpointerup={onPointerUp}
	onpointercancel={onPointerUp}
	onkeydown={onKeyDown}
>
	{#if peaks.length === 0}
		<div class="absolute top-1/2 right-0 left-0 h-px bg-wave-idle"></div>
	{:else}
		<svg class="h-full w-full" viewBox="0 0 880 32" preserveAspectRatio="none" aria-hidden="true">
			{#each bars as value, index (index)}
				<rect
					x={index * 10 + 1}
					y={16 - Math.max(2, value * 30) / 2}
					width="6"
					height={Math.max(2, value * 30)}
					rx="1"
					class={index / BARS <= progress ? 'fill-signal' : 'fill-wave-idle'}
				/>
			{/each}
		</svg>
	{/if}
	<div
		class="pointer-events-none absolute top-0 bottom-0 w-px bg-signal"
		style={`left: ${clamp(progress, 0, 1) * 100}%`}
	></div>
</div>
