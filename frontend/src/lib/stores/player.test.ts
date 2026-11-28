import { expect, test, vi } from 'vitest';
import { ApiError } from '#lib/api/client.js';
import { makeClip } from '#lib/test-fixtures.js';
import { createPlayer, type AudioLike, type PlayerDeps } from './player.svelte.js';

class FakeAudio implements AudioLike {
	src = '';
	currentTime = 0;
	duration = 6.15;
	playbackRate = 1;
	volume = 1;
	paused = true;
	private listeners = new Map<string, () => void>();

	play(): Promise<void> {
		this.paused = false;
		this.emit('play');
		return Promise.resolve();
	}

	pause(): void {
		this.paused = true;
		this.emit('pause');
	}

	addEventListener(type: string, listener: () => void): void {
		this.listeners.set(type, listener);
	}

	emit(type: string): void {
		this.listeners.get(type)?.();
	}
}

function deps(fetchAudio?: PlayerDeps['fetchAudio']): { deps: PlayerDeps; audio: FakeAudio } {
	const audio = new FakeAudio();
	return {
		audio,
		deps: {
			fetchAudio: fetchAudio ?? vi.fn().mockResolvedValue(new Blob(['audio'])),
			createAudio: () => audio,
			decode: vi.fn().mockResolvedValue([0.2, 0.8, 1]),
			createObjectURL: () => 'blob:audio',
			revokeObjectURL: vi.fn()
		}
	};
}

test('loads a clip, keeps time bounded and controls playback', async () => {
	const { deps: playerDeps, audio } = deps();
	const player = createPlayer(playerDeps);
	await player.play(makeClip());
	expect(player.clip?.id).toBe('clip-1');
	expect(player.playing).toBe(true);
	await vi.waitFor(() => expect(player.peaks).toEqual([0.2, 0.8, 1]));

	audio.currentTime = 6.5;
	audio.emit('loadedmetadata');
	audio.emit('timeupdate');
	expect(player.currentTime).toBe(6.15);

	player.toggle();
	expect(player.playing).toBe(false);
	player.seek(0.5);
	expect(player.currentTime).toBeCloseTo(3.075);
	player.setRate(1.5);
	expect(player.rate).toBe(1.5);
	player.setVolume(0.2);
	expect(player.volume).toBe(0.2);

	player.close();
	expect(player.clip).toBeNull();
	expect(player.playing).toBe(false);
	expect(playerDeps.revokeObjectURL).toHaveBeenCalled();
});

test('turns a 401 into a clear daemon-key message', async () => {
	const { deps: playerDeps } = deps(
		vi.fn().mockRejectedValue(
			new ApiError({
				status: 401,
				code: 'UNAUTHORIZED',
				message: 'Unauthorized',
				suggestion: '',
				correlationId: ''
			})
		) as PlayerDeps['fetchAudio']
	);
	const player = createPlayer(playerDeps);
	await player.play(makeClip());
	expect(player.error).toContain('daemon API key');
	expect(player.loading).toBe(false);
});
