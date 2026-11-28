<script lang="ts">
	import { clamp, formatClock } from '#lib/format.js';

	let {
		peaks,
		duration,
		start,
		end,
		minSeconds = 0.5,
		label = 'Trim selection',
		onchange
	}: {
		peaks: number[];
		duration: number;
		start: number;
		end: number;
		minSeconds?: number;
		label?: string;
		onchange: (start: number, end: number) => void;
	} = $props();

	const BARS = 96;
	let dragging = $state<'start' | 'end' | null>(null);
	let trackEl = $state<HTMLDivElement | null>(null);

	const startRatio = $derived(duration > 0 ? clamp(start / duration, 0, 1) : 0);
	const endRatio = $derived(duration > 0 ? clamp(end / duration, 0, 1) : 1);

	const barViews = $derived(
		Array.from({ length: BARS }, (_, index) => {
			const value = peaks[index];
			const magnitude = typeof value === 'number' ? clamp(value, 0.05, 1) : 0.3;
			const ratio = (index + 0.5) / BARS;
			return {
				x: index * 10 + 1,
				y: 40 - Math.max(2, magnitude * 66) / 2,
				height: Math.max(2, magnitude * 66),
				inside: ratio >= startRatio && ratio <= endRatio
			};
		})
	);

	function setStart(next: number): void {
		onchange(clamp(next, 0, end - minSeconds), end);
	}

	function setEnd(next: number): void {
		onchange(start, clamp(next, start + minSeconds, duration));
	}

	function ratioAt(clientX: number): number {
		if (!trackEl) return 0;
		const rect = trackEl.getBoundingClientRect();
		if (rect.width <= 0) return 0;
		return clamp((clientX - rect.left) / rect.width, 0, 1);
	}

	function onPointerDown(handle: 'start' | 'end', event: PointerEvent): void {
		event.preventDefault();
		dragging = handle;
		(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
	}

	function onPointerMove(event: PointerEvent): void {
		if (!dragging) return;
		const seconds = ratioAt(event.clientX) * duration;
		if (dragging === 'start') setStart(seconds);
		else setEnd(seconds);
	}

	function onPointerUp(event: PointerEvent): void {
		if (!dragging) return;
		dragging = null;
		(event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
	}

	function onKeyDown(handle: 'start' | 'end', event: KeyboardEvent): void {
		const step = event.shiftKey ? 0.5 : 0.05;
		const current = handle === 'start' ? start : end;
		let next: number | null = null;
		if (event.key === 'ArrowLeft') next = current - step;
		if (event.key === 'ArrowRight') next = current + step;
		if (event.key === 'Home') next = handle === 'start' ? 0 : start + minSeconds;
		if (event.key === 'End') next = handle === 'start' ? end - minSeconds : duration;
		if (next === null) return;
		event.preventDefault();
		if (handle === 'start') setStart(next);
		else setEnd(next);
	}
</script>

<div class="space-y-2">
	<div
		class="relative h-20 touch-none rounded-control border border-rule bg-surface select-none"
		bind:this={trackEl}
	>
		<svg
			class="absolute inset-0 h-full w-full"
			viewBox="0 0 960 80"
			preserveAspectRatio="none"
			aria-hidden="true"
		>
			{#each barViews as bar (bar.x)}
				<rect
					x={bar.x}
					y={bar.y}
					width="6"
					height={bar.height}
					rx="1"
					class={bar.inside ? 'fill-signal' : 'fill-wave-idle'}
					opacity={bar.inside ? 1 : 0.55}
				/>
			{/each}
		</svg>

		{#if duration > 0}
			<div
				class="pointer-events-none absolute top-0 bottom-0 bg-signal-soft"
				style={`left: ${startRatio * 100}%; width: ${Math.max(0, (endRatio - startRatio) * 100)}%`}
			></div>
			<div
				class="pointer-events-none absolute top-0 bottom-0 w-px bg-signal"
				style={`left: ${startRatio * 100}%`}
			></div>
			<div
				class="pointer-events-none absolute top-0 bottom-0 w-px bg-signal"
				style={`left: ${endRatio * 100}%`}
			></div>

			<div
				role="slider"
				tabindex="0"
				aria-label={`${label} start`}
				aria-valuemin={0}
				aria-valuemax={Math.round(duration * 1000)}
				aria-valuenow={Math.round(start * 1000)}
				aria-valuetext={formatClock(start)}
				class="absolute top-0 bottom-0 -left-3 w-6 cursor-ew-resize touch-none"
				style={`left: ${startRatio * 100}%`}
				onpointerdown={(event) => onPointerDown('start', event)}
				onpointermove={onPointerMove}
				onpointerup={onPointerUp}
				onpointercancel={onPointerUp}
				onkeydown={(event) => onKeyDown('start', event)}
			>
				<span
					class="absolute top-1/2 left-1/2 h-10 w-2 -translate-x-1/2 -translate-y-1/2 rounded-[2px] border border-signal bg-panel"
				></span>
			</div>

			<div
				role="slider"
				tabindex="0"
				aria-label={`${label} end`}
				aria-valuemin={0}
				aria-valuemax={Math.round(duration * 1000)}
				aria-valuenow={Math.round(end * 1000)}
				aria-valuetext={formatClock(end)}
				class="absolute top-0 bottom-0 -left-3 w-6 cursor-ew-resize touch-none"
				style={`left: ${endRatio * 100}%`}
				onpointerdown={(event) => onPointerDown('end', event)}
				onpointermove={onPointerMove}
				onpointerup={onPointerUp}
				onpointercancel={onPointerUp}
				onkeydown={(event) => onKeyDown('end', event)}
			>
				<span
					class="absolute top-1/2 left-1/2 h-10 w-2 -translate-x-1/2 -translate-y-1/2 rounded-[2px] border border-signal bg-panel"
				></span>
			</div>
		{/if}
	</div>

	<div class="flex flex-wrap items-end gap-3">
		<div class="space-y-1">
			<label class="field-label" for="trim-start">Start (s)</label>
			<input
				id="trim-start"
				class="input metric h-8 w-[92px] text-[12px]"
				type="number"
				min="0"
				max={Math.max(0, duration)}
				step="0.05"
				value={start.toFixed(2)}
				onchange={(event) => setStart(Number(event.currentTarget.value))}
			/>
		</div>
		<div class="space-y-1">
			<label class="field-label" for="trim-end">End (s)</label>
			<input
				id="trim-end"
				class="input metric h-8 w-[92px] text-[12px]"
				type="number"
				min="0"
				max={Math.max(0, duration)}
				step="0.05"
				value={end.toFixed(2)}
				onchange={(event) => setEnd(Number(event.currentTarget.value))}
			/>
		</div>
		<p class="metric pb-1.5 text-[11px] text-ink-secondary">
			Selection {formatClock(Math.max(0, end - start), 2)} of {formatClock(duration, 2)}
		</p>
		<button type="button" class="btn btn-sm ml-auto" onclick={() => onchange(0, duration)}>
			Full clip
		</button>
	</div>
	<p class="text-[11px] leading-4 text-ink-muted">
		Drag the two brass handles or use the arrow keys (Shift for 0.5 s steps). The numbers work too.
	</p>
</div>
