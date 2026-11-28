import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const routes = [
	['Speak', '/ui/speak'],
	['Voices', '/ui/voices'],
	['Transcribe', '/ui/transcribe'],
	['Library', '/ui/library'],
	['Models', '/ui/models'],
	['Settings', '/ui/settings']
] as const;

test.beforeAll(() => {
	execFileSync('uv', ['run', 'python', 'scripts/seed_demo.py'], {
		cwd: '..',
		env: { ...process.env, AGENT_VOICE_DEMO_URL: 'http://127.0.0.1:8765' },
		stdio: 'inherit'
	});
});

for (const [name, path] of routes) {
	test(`${name} has no serious accessibility violations`, async ({ page }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		const violations: string[] = [];
		for (const theme of ['dark', 'light'] as const) {
			await page.goto(path);
			await page.evaluate((mode) => localStorage.setItem('agent-voice-theme', mode), theme);
			await page.reload();
			await expect(page.getByTestId('top-bar')).toBeVisible();
			await page.waitForTimeout(200);
			const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
			violations.push(
				...result.violations
					.filter((violation) => violation.impact === 'serious' || violation.impact === 'critical')
					.map((violation) => `${theme}: ${violation.id} — ${violation.help}`)
			);
		}
		expect(violations).toEqual([]);
	});
}
