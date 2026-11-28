import { defineConfig, devices } from '@playwright/test';

const port = 8765;

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
			args: [
				'--autoplay-policy=no-user-gesture-required',
				'--use-fake-ui-for-media-stream',
				'--use-fake-device-for-media-stream'
			]
		}
	},
	webServer: [
		{
			command: `cd .. && rm -rf /tmp/agent-voice-qa-data && AGENT_VOICE_API_KEY= AGENT_VOICE_HOST=127.0.0.1 AGENT_VOICE_PORT=${port} AGENT_VOICE_DATA_DIR=/tmp/agent-voice-qa-data AGENT_VOICE_DEFAULT_ENGINE=mock AGENT_VOICE_DEFAULT_ASR_ENGINE=mock_asr AGENT_VOICE_UI=on uv run agent-voice serve`,
			url: `http://127.0.0.1:${port}/ui/`,
			reuseExistingServer: false,
			timeout: 120_000
		}
	],
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }]
});
