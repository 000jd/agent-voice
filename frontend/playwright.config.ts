import { defineConfig, devices } from '@playwright/test';

const port = 4173;

export default defineConfig({
	testDir: './e2e',
	fullyParallel: true,
	forbidOnly: Boolean(process.env.CI),
	retries: process.env.CI ? 2 : 0,
	reporter: 'list',
	use: {
		baseURL: `http://127.0.0.1:${port}`,
		trace: 'on-first-retry'
	},
	webServer: {
		command: `bun run dev -- --port ${port}`,
		url: `http://127.0.0.1:${port}/ui/`,
		reuseExistingServer: !process.env.CI,
		timeout: 120_000
	},
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }]
});
