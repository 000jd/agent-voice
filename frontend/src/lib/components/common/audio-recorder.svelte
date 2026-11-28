<script lang="ts">
	import { onDestroy } from 'svelte';
	import Mic from '@lucide/svelte/icons/mic';
	import Square from '@lucide/svelte/icons/square';
	import RotateCcw from '@lucide/svelte/icons/rotate-ccw';
	import { formatClock } from '#lib/format.js';

	let {
		maxSeconds = 60,
		onrecorded,
		disabled = false
	}: {
		maxSeconds?: number;
		onrecorded: (blob: Blob) => void;
		disabled?: boolean;
	} = $props();

	const supported = $derived(
		typeof window !== 'undefined' &&
			typeof MediaRecorder !== 'undefined' &&
			typeof navigator !== 'undefined' &&
			typeof navigator.mediaDevices?.getUserMedia === 'function'
	);

	let phase = $state<'idle' | 'countdown' | 'recording' | 'review'>('idle');
	let countdown = $state(3);
	let elapsed = $state(0);
	let level = $state(0);
	let error = $state('');
	let recordedUrl = $state<string | null>(null);

	let stream: MediaStream | null = null;
	let recorder: MediaRecorder | null = null;
	let chunks: Blob[] = [];
	let context: AudioContext | null = null;
	let analyser: AnalyserNode | null = null;
	let levelTimer: ReturnType<typeof setInterval> | null = null;
	let elapsedTimer: ReturnType<typeof setInterval> | null = null;
	let countdownTimer: ReturnType<typeof setTimeout> | null = null;
	let recordedBlob: Blob | null = null;

	const meterBars = $derived(
		Array.from({ length: 48 }, (_, tick) => ({
			tick,
			height: Math.max(2, Math.round(Math.abs(Math.sin(tick * 0.7)) * 100)),
			active: phase === 'review' || tick / 48 < level * 1.6
		}))
	);

	function pickMimeType(): string {
		if (typeof MediaRecorder === 'undefined') return '';
		const options = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
		return options.find((type) => MediaRecorder.isTypeSupported?.(type)) ?? '';
	}

	async function begin(): Promise<void> {
		error = '';
		if (!supported) {
			error = 'This browser cannot record audio. Upload a file instead.';
			return;
		}
		try {
			stream = await navigator.mediaDevices.getUserMedia({ audio: true });
		} catch {
			error = 'Microphone permission was denied. Allow it in the browser, or upload a file.';
			return;
		}
		phase = 'countdown';
		countdown = 3;
		const tick = (): void => {
			countdown -= 1;
			if (countdown > 0) {
				countdownTimer = setTimeout(tick, 1000);
				return;
			}
			countdownTimer = null;
			startRecording();
		};
		countdownTimer = setTimeout(tick, 1000);
	}

	function startRecording(): void {
		if (!stream) return;
		const mimeType = pickMimeType();
		chunks = [];
		try {
			recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
		} catch {
			error = 'This browser refused the recording format. Upload a file instead.';
			cleanupStream();
			phase = 'idle';
			return;
		}
		recorder.ondataavailable = (event) => {
			if (event.data.size > 0) chunks.push(event.data);
		};
		recorder.onstop = () => {
			recordedBlob = new Blob(chunks, { type: recorder?.mimeType || 'audio/webm' });
			if (recordedUrl) URL.revokeObjectURL(recordedUrl);
			recordedUrl = URL.createObjectURL(recordedBlob);
			phase = 'review';
			stopMeters();
			cleanupStream();
		};
		recorder.start(250);
		phase = 'recording';
		elapsed = 0;
		elapsedTimer = setInterval(() => {
			elapsed += 0.1;
			if (elapsed >= maxSeconds) stop();
		}, 100);
		startMeter();
	}

	function startMeter(): void {
		if (!stream) return;
		try {
			context = new AudioContext();
			const source = context.createMediaStreamSource(stream);
			analyser = context.createAnalyser();
			analyser.fftSize = 1024;
			source.connect(analyser);
			const data = new Float32Array(analyser.fftSize);
			levelTimer = setInterval(() => {
				if (!analyser) return;
				analyser.getFloatTimeDomainData(data);
				let sum = 0;
				for (const value of data) sum += value * value;
				level = Math.min(1, Math.sqrt(sum / data.length) * 4);
			}, 80);
		} catch {
			// A level meter is optional; recording continues without it.
			level = 0;
		}
	}

	function stopMeters(): void {
		if (levelTimer !== null) clearInterval(levelTimer);
		if (elapsedTimer !== null) clearInterval(elapsedTimer);
		levelTimer = null;
		elapsedTimer = null;
		level = 0;
		void context?.close();
		context = null;
		analyser = null;
	}

	function cleanupStream(): void {
		for (const track of stream?.getTracks() ?? []) track.stop();
		stream = null;
	}

	function stop(): void {
		if (recorder && recorder.state !== 'inactive') recorder.stop();
		else {
			stopMeters();
			cleanupStream();
			phase = 'idle';
		}
	}

	function retake(): void {
		if (recordedUrl) URL.revokeObjectURL(recordedUrl);
		recordedUrl = null;
		recordedBlob = null;
		phase = 'idle';
		elapsed = 0;
		error = '';
	}

	function accept(): void {
		if (recordedBlob) onrecorded(recordedBlob);
	}

	function cancelCountdown(): void {
		if (countdownTimer) clearTimeout(countdownTimer);
		countdownTimer = null;
		stopMeters();
		cleanupStream();
		phase = 'idle';
	}

	onDestroy(() => {
		stopMeters();
		cleanupStream();
		if (countdownTimer) clearTimeout(countdownTimer);
		if (recordedUrl) URL.revokeObjectURL(recordedUrl);
	});
</script>

<div class="space-y-2">
	{#if !supported}
		<p
			class="rounded-control border border-rule bg-surface px-3 py-2 text-[11px] leading-4 text-ink-secondary"
		>
			Recording needs <code class="metric">MediaRecorder</code> and a microphone. This browser (WebKitGTK
			and Safari among them) does not expose both, so record elsewhere and upload the file instead.
		</p>
	{:else}
		<div class="flex flex-wrap items-center gap-2">
			{#if phase === 'idle'}
				<button type="button" class="btn" {disabled} onclick={() => void begin()}>
					<Mic class="size-4" aria-hidden="true" />
					Record
				</button>
			{:else if phase === 'countdown'}
				<button type="button" class="btn btn-danger" onclick={cancelCountdown}>
					Cancel countdown
				</button>
				<span class="text-[12px] text-ink-secondary" aria-live="polite">
					Recording starts in {countdown}…
				</span>
			{:else if phase === 'recording'}
				<button type="button" class="btn btn-danger" onclick={stop}>
					<Square class="size-3.5" aria-hidden="true" />
					Stop
				</button>
				<span class="metric text-[12px] text-ink-secondary">
					{formatClock(elapsed, 1)} / {formatClock(maxSeconds, 0)}
				</span>
				<span class="text-[12px] text-ink-secondary" aria-live="polite">
					{elapsed < maxSeconds * 0.25 ? 'Recording…' : 'Recording, keep going.'}
				</span>
			{:else}
				<button type="button" class="btn btn-ghost" onclick={retake}>
					<RotateCcw class="size-3.5" aria-hidden="true" />
					Retake
				</button>
				<button type="button" class="btn btn-primary" onclick={accept}>Use recording</button>
				<span class="metric text-[12px] text-ink-secondary">{formatClock(elapsed, 1)}</span>
			{/if}
		</div>

		{#if phase === 'recording' || phase === 'review'}
			<div class="flex h-8 items-end gap-[2px]" aria-hidden="true">
				{#each meterBars as bar (bar.tick)}
					<span
						class={`flex-1 rounded-[1px] ${bar.active ? 'bg-signal' : 'bg-wave-idle'}`}
						style={`height: ${bar.height}%`}
					></span>
				{/each}
			</div>
		{/if}

		{#if recordedUrl}
			<audio class="h-8 w-full" src={recordedUrl} controls preload="metadata"></audio>
		{/if}
	{/if}

	{#if error}
		<p class="text-[11px] leading-4 text-danger" role="alert">{error}</p>
	{/if}

	<p class="text-[11px] leading-4 text-ink-muted">
		The microphone is opened only when you press Record. The clip goes to your daemon and nowhere
		else.
	</p>
</div>
