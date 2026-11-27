import { render, screen } from '@testing-library/svelte';
import { beforeEach, expect, test, vi } from 'vitest';
import { loadDashboard, type DashboardData } from '#lib/api/dashboard.js';
import Page from './+page.svelte';

vi.mock('#lib/api/dashboard.js', () => ({
	loadDashboard: vi.fn()
}));

const load = vi.mocked(loadDashboard);

const ready: DashboardData = {
	health: { status: 'ok', version: '0.6.0', protocolVersion: 1, torchLoaded: false },
	engines: [
		{
			id: 'mock',
			name: 'Mock engine',
			backend: 'builtin',
			backendVersion: '0.6.0',
			installed: true,
			kind: 'tts',
			device: 'cpu',
			resolvedDevice: 'cpu',
			models: [
				{
					alias: 'mock-voice',
					qualityTier: 'draft',
					downloaded: true,
					loaded: false
				}
			],
			whyUnavailable: null
		}
	]
};

beforeEach(() => {
	load.mockReset();
});

test('shows the loading state while the daemon is queried', () => {
	load.mockReturnValue(new Promise<DashboardData>(() => {}));
	render(Page);

	expect(screen.getByLabelText('Loading dashboard')).toBeInTheDocument();
});

test('shows a recoverable error when the daemon is unreachable', async () => {
	load.mockRejectedValue(new Error('The daemon is not reachable.'));
	render(Page);

	expect(await screen.findByText('Daemon not reachable')).toBeInTheDocument();
	expect(screen.getByText('The daemon is not reachable.')).toBeInTheDocument();
	expect(screen.getByText('agent-voice serve')).toBeInTheDocument();
});

test('renders the daemon and engine state on success', async () => {
	load.mockResolvedValue(ready);
	render(Page);

	expect(await screen.findByText('Mock engine')).toBeInTheDocument();
	expect(screen.getByText('Protocol v1')).toBeInTheDocument();
	expect(screen.getByText('mock-voice')).toBeInTheDocument();
	expect(screen.getAllByText('0.6.0').length).toBeGreaterThan(0);
});
