import { ApiError, requestBlob } from '#lib/api/client.js';
import type { ClipSummary } from '#lib/api/types.js';
import { clamp, sanitizeFilename } from '#lib/format.js';

export type AudioLike = {
	src: string;
	currentTime: number;
	duration: number;
	playbackRate: number;
	volume: number;
	paused: boolean;
	play: () => Promise<void>;
	pause: () => void;
	addEventListener: (type: string, listener: () => void) => void;
};

export type PlayerDeps = {
	fetchAudio: (url: string) => Promise<Blob>;
	createAudio: () => AudioLike;
	decode: (blob: Blob) => Promise<number[]>;
	createObjectURL: (blob: Blob) => string;
	revokeObjectURL: (url: string) => void;
};

function audioErrorMessage(error: unknown): string {
	if (error instanceof ApiError) {
		if (error.status === 401) {
			return 'Audio is protected by the daemon API key. Enter the key to play it.';
		}
		return error.suggestion ? `${error.message} ${error.suggestion}` : error.message;
	}
	if (error instanceof Error) return error.message;
	return 'Audio could not be loaded.';
}

async function decodePeaks(blob: Blob): Promise<number[]> {
	if (typeof window === 'undefined' || typeof window.AudioContext === 'undefined') return [];
	const context = new AudioContext();
	try {
		const buffer = await context.decodeAudioData(await blob.arrayBuffer());
		const bars = 96;
		const channels = Array.from({ length: buffer.numberOfChannels }, (_, index) =>
			buffer.getChannelData(index)
		);
		const peaks: number[] = [];
		const bucket = Math.max(1, Math.floor(buffer.length / bars));
		for (let bar = 0; bar < bars; bar += 1) {
			let peak = 0;
			const start = bar * bucket;
			const end = Math.min(buffer.length, start + bucket);
			for (const channel of channels) {
				for (let index = start; index < end; index += 1) {
					const value = Math.abs(channel[index] ?? 0);
					if (value > peak) peak = value;
				}
			}
			peaks.push(peak);
		}
		const loudest = peaks.reduce((max, value) => Math.max(max, value), 0) || 1;
		return peaks.map((value) => Math.max(0.06, value / loudest));
	} catch {
		return [];
	} finally {
		void context.close();
	}
}

function browserDeps(): PlayerDeps {
	return {
		fetchAudio: requestBlob,
		createAudio: () => new Audio(),
		decode: decodePeaks,
		createObjectURL: (blob) => URL.createObjectURL(blob),
		revokeObjectURL: (url) => URL.revokeObjectURL(url)
	};
}

export class Player {
	clip = $state<ClipSummary | null>(null);
	playing = $state(false);
	loading = $state(false);
	error = $state('');
	currentTime = $state(0);
	duration = $state(0);
	peaks = $state<number[]>([]);
	rate = $state(1);
	volume = $state(1);

	private deps: PlayerDeps;
	private audio: AudioLike | null = null;
	private objectUrl: string | null = null;
	private loadToken = 0;

	constructor(deps: PlayerDeps) {
		this.deps = deps;
	}

	get progress(): number {
		if (this.duration <= 0) return 0;
		return clamp(this.currentTime / this.duration, 0, 1);
	}

	get active(): boolean {
		return this.clip !== null;
	}

	async play(clip: ClipSummary): Promise<void> {
		if (this.clip?.id === clip.id && this.audio) {
			if (!this.playing) await this.start();
			return;
		}
		await this.load(clip);
	}

	private async load(clip: ClipSummary): Promise<void> {
		const token = ++this.loadToken;
		this.clearAudio();
		this.clip = clip;
		this.currentTime = 0;
		this.duration = clip.durationSec || 0;
		this.peaks = [];
		this.error = '';
		this.loading = true;
		try {
			const blob = await this.deps.fetchAudio(clip.url);
			if (token !== this.loadToken) return;
			const objectUrl = this.deps.createObjectURL(blob);
			this.objectUrl = objectUrl;
			const audio = this.deps.createAudio();
			audio.src = objectUrl;
			audio.playbackRate = this.rate;
			audio.volume = this.volume;
			this.audio = audio;
			this.attach(audio);
			void this.deps
				.decode(blob)
				.then((peaks) => {
					if (token === this.loadToken) this.peaks = peaks;
				})
				.catch(() => undefined);
			await this.start();
		} catch (error) {
			if (token === this.loadToken) {
				this.error = audioErrorMessage(error);
				this.loading = false;
				this.playing = false;
			}
		}
	}

	private attach(audio: AudioLike): void {
		audio.addEventListener('timeupdate', () => {
			if (Number.isFinite(audio.duration) && audio.duration > 0) this.duration = audio.duration;
			this.currentTime =
				this.duration > 0 ? Math.min(audio.currentTime, this.duration) : audio.currentTime;
		});
		audio.addEventListener('loadedmetadata', () => {
			if (Number.isFinite(audio.duration) && audio.duration > 0) this.duration = audio.duration;
		});
		audio.addEventListener('ended', () => {
			this.playing = false;
			this.currentTime = this.duration;
		});
		audio.addEventListener('play', () => {
			this.playing = true;
		});
		audio.addEventListener('pause', () => {
			this.playing = false;
		});
		audio.addEventListener('error', () => {
			this.error = 'Audio could not be played by this browser.';
			this.playing = false;
		});
	}

	private async start(): Promise<void> {
		if (!this.audio) return;
		this.error = '';
		try {
			await this.audio.play();
			this.playing = true;
		} catch (error) {
			this.error = audioErrorMessage(error);
			this.playing = false;
		} finally {
			this.loading = false;
		}
	}

	toggle(): void {
		if (!this.audio) return;
		if (this.playing) {
			this.audio.pause();
			this.playing = false;
			return;
		}
		void this.start();
	}

	seek(ratio: number): void {
		const next = clamp(ratio, 0, 1) * (this.duration || 0);
		this.seekSeconds(next);
	}

	seekSeconds(seconds: number): void {
		const next = clamp(seconds, 0, this.duration || 0);
		this.currentTime = next;
		if (this.audio) this.audio.currentTime = next;
	}

	setRate(rate: number): void {
		this.rate = clamp(rate, 0.5, 2);
		if (this.audio) this.audio.playbackRate = this.rate;
	}

	setVolume(volume: number): void {
		this.volume = clamp(volume, 0, 1);
		if (this.audio) this.audio.volume = this.volume;
	}

	async download(target?: ClipSummary): Promise<void> {
		const clip = target ?? this.clip;
		if (!clip) return;
		try {
			const blob = await this.deps.fetchAudio(clip.url);
			const url = this.deps.createObjectURL(blob);
			const link = document.createElement('a');
			link.href = url;
			link.download = `${sanitizeFilename(clip.title)}.${clip.format || 'wav'}`;
			link.click();
			window.setTimeout(() => this.deps.revokeObjectURL(url), 1000);
		} catch (error) {
			this.error = audioErrorMessage(error);
		}
	}

	close(): void {
		this.loadToken += 1;
		this.clearAudio();
		this.clip = null;
		this.playing = false;
		this.loading = false;
		this.error = '';
		this.currentTime = 0;
		this.duration = 0;
		this.peaks = [];
	}

	private clearAudio(): void {
		if (this.audio) {
			this.audio.pause();
			this.audio.src = '';
			this.audio = null;
		}
		if (this.objectUrl) {
			this.deps.revokeObjectURL(this.objectUrl);
			this.objectUrl = null;
		}
	}
}

export function createPlayer(overrides: Partial<PlayerDeps> = {}): Player {
	return new Player({ ...browserDeps(), ...overrides });
}

let singleton: Player | null = null;

export function getPlayer(): Player {
	singleton ??= createPlayer();
	return singleton;
}
