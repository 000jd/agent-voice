import { defineConfig, devices } from '@playwright/test';

const port = 4173;
const apiPort = 8765;

export default defineConfig({
	testDir: './e2e',
	fullyParallel: false,
	workers: 1,
	forbidOnly: Boolean(process.env.CI),
	retries: process.env.CI ? 2 : 0,
	reporter: 'list',
	use: {
		baseURL: `http://127.0.0.1:${port}`,
		trace: 'on-first-retry',
		launchOptions: {
			args: ['--autoplay-policy=no-user-gesture-required']
		}
	},
	webServer: [
		{
			command: `cd .. && rm -rf /tmp/agent-voice-qa-data && AGENT_VOICE_API_KEY= AGENT_VOICE_HOST=127.0.0.1 AGENT_VOICE_PORT=${apiPort} AGENT_VOICE_DATA_DIR=/tmp/agent-voice-qa-data AGENT_VOICE_DEFAULT_ENGINE=mock AGENT_VOICE_DEFAULT_ASR_ENGINE=mock_asr uv run agent-voice serve`,
			url: `http://127.0.0.1:${apiPort}/health`,
			reuseExistingServer: false,
			timeout: 120_000
		},
		{
			command: `AGENT_VOICE_DEV_URL=http://127.0.0.1:${apiPort} bun run dev -- --port ${port}`,
			url: `http://127.0.0.1:${port}/ui/`,
			reuseExistingServer: !process.env.CI,
			timeout: 120_000
		}
	],
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }]
});
