import { render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { beforeEach, expect, test, vi } from 'vitest';
import type { ClipSummary } from '#lib/api/types.js';
import { studio } from '#lib/stores/studio.svelte.js';
import Page from './+page.svelte';

vi.mock('#lib/api/studio.js', () => ({
	getHealth: vi.fn().mockResolvedValue({
		status: 'ok',
		version: '0.6.0',
		protocolVersion: 1,
		torchLoaded: false
	}),
	getGpu: vi.fn().mockResolvedValue({
		activeEngine: null,
		loadedModels: [],
		workers: {},
		cpuEngines: [],
		workerDevices: {},
		workerVramMb: {},
		vramSource: 'none',
		vramUsedMb: null,
		vramFreeMb: null,
		queueDepth: 0,
		inFlight: 0,
		unloadInSeconds: null,
		lastUsedAt: null
	}),
	getEngines: vi.fn().mockResolvedValue([]),
	getVoices: vi.fn().mockResolvedValue([]),
	getModels: vi.fn(),
	describeApiError: (error: unknown) => ({
		status: 0,
		code: 'CLIENT_ERROR',
		message: error instanceof Error ? error.message : String(error),
		suggestion: ''
	}),
	ApiError: class ApiError extends Error {},
	deleteVoice: vi.fn(),
	unloadModels: vi.fn(),
	fetchAudioBlob: vi.fn().mockResolvedValue(new Blob())
}));

const clip: ClipSummary = {
	id: 'abc123',
	title: 'A quiet test render',
	subtitle: 'mock · mock-tone',
	url: '/audio/abc123',
	durationSec: 2.4,
	engineId: 'mock',
	modelAlias: 'mock-tone',
	cached: true,
	elapsedMs: 10,
	realtimeMultiple: null,
	format: 'wav',
	createdAt: Date.now(),
	voiceId: null,
	speaker: 'Ryan',
	mode: 'custom_voice'
};

beforeEach(() => {
	studio.sessionClips = [];
	studio.connection = 'connected';
	studio.health = { status: 'ok', version: '0.6.0', protocolVersion: 1, torchLoaded: false };
});

test('renders the studio status strip and empty session state', async () => {
	render(Page);
	await tick();
	expect(screen.getByRole('heading', { name: 'Studio' })).toBeInTheDocument();
	expect(screen.getByText(/Connected/)).toBeInTheDocument();
	expect(screen.getByText('No renders in this session yet.')).toBeInTheDocument();
});

test('renders recent session clips', async () => {
	studio.sessionClips = [clip];
	render(Page);
	await tick();
	expect(screen.getByText('A quiet test render')).toBeInTheDocument();
	expect(screen.getByText(/2\.4s|0:02\.40/)).toBeInTheDocument();
});
