import { mkdirSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const out = '/tmp/agent-voice-qa';
const routes = [
	['dashboard', '/ui/'],
	['speak', '/ui/speak'],
	['voices', '/ui/voices'],
	['models', '/ui/models'],
	['transcribe', '/ui/transcribe'],
	['history', '/ui/history'],
	['settings', '/ui/settings']
] as const;

test.beforeAll(() => {
	mkdirSync(out, { recursive: true });
});

test('captures a populated studio with a real render', async ({ page, request }) => {
	await request.post('/v1/voices/design', {
		data: {
			name: 'visual-narrator',
			instruct: 'warm, measured, late-night studio narration',
			sample_text: 'The studio is quiet, and the first take is ready to hear.',
			language: 'English',
			save_as_clone: true,
			candidates: 1,
			overwrite: true,
			engine: 'mock',
			model: 'mock-design',
			quality: 'high'
		}
	});

	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/ui/speak');
	await page.locator('#speak-engine').selectOption('mock');
	await page.getByRole('button', { name: 'Preset', exact: true }).click();
	await page.locator('#speak-model').selectOption('mock-custom');
	await page.locator('#speak-speaker').selectOption('Ryan');
	await page
		.getByLabel('Speech text')
		.fill('The first take is in. A quiet room, a measured pace, and one line to keep.');
	const audioResponse = page.waitForResponse(
		(response) => response.url().includes('/audio/') && response.status() === 200
	);
	await page.getByRole('button', { name: 'Generate speech' }).click();
	await audioResponse;
	await expect(page.getByTestId('player-bar')).toBeVisible();
	await page.screenshot({
		path: `${out}/desktop-dark-speak-populated.png`,
		fullPage: false,
		animations: 'disabled'
	});

	await page.locator('aside').getByRole('link', { name: 'Voices', exact: true }).click();
	await expect(page.getByRole('main').getByRole('heading', { name: 'Voices' })).toBeVisible();
	await page.screenshot({
		path: `${out}/desktop-dark-voices-populated.png`,
		fullPage: false,
		animations: 'disabled'
	});

	await page.locator('aside').getByRole('link', { name: 'Dashboard', exact: true }).click();
	await expect(page.getByRole('main').getByRole('heading', { name: 'Dashboard' })).toBeVisible();
	await expect(
		page
			.getByRole('main')
			.getByText(/The first take is in/)
			.first()
	).toBeVisible();
	await page.screenshot({
		path: `${out}/desktop-dark-dashboard-populated.png`,
		fullPage: false,
		animations: 'disabled'
	});

	await page.getByRole('button', { name: 'Switch to light theme' }).click();
	await page.waitForTimeout(180);
	await page.waitForTimeout(320);
	await page
		.getByTestId('player-bar')
		.screenshot({ path: `${out}/player-element-light.png`, animations: 'disabled' });
	await page.getByRole('button', { name: 'Switch to dark theme' }).click();
	await page.waitForTimeout(180);

	await page.setViewportSize({ width: 390, height: 844 });
	await page.screenshot({
		path: `${out}/mobile-dark-dashboard-populated.png`,
		fullPage: false,
		animations: 'disabled'
	});
	await page.getByRole('button', { name: 'Open navigation' }).click();
	await page.getByRole('link', { name: 'Speak', exact: true }).click();
	await expect(page).toHaveURL(/\/ui\/speak$/);
	await expect(
		page.getByRole('main').getByRole('heading', { name: 'Speak', level: 1 })
	).toBeVisible();
	await page.screenshot({
		path: `${out}/mobile-dark-speak-populated.png`,
		fullPage: false,
		animations: 'disabled'
	});
});

test('captures the studio at desktop and mobile widths', async ({ page }) => {
	await page.setViewportSize({ width: 1440, height: 900 });
	for (const [name, path] of routes) {
		await page.goto(path);
		await expect(page.getByRole('main').getByRole('heading').first()).toBeVisible();
		const overflow = await page.evaluate(
			() => document.documentElement.scrollWidth - window.innerWidth
		);
		expect(overflow, `${name} horizontal overflow`).toBeLessThanOrEqual(1);
		await page.screenshot({
			path: `${out}/desktop-dark-${name}.png`,
			fullPage: true,
			animations: 'disabled'
		});
	}

	await page.evaluate(() => localStorage.setItem('agent-voice-theme', 'light'));
	await page.goto('/ui/');
	await expect(page.getByRole('main').getByRole('heading').first()).toBeVisible();
	await page.screenshot({
		path: `${out}/desktop-light-dashboard.png`,
		fullPage: true,
		animations: 'disabled'
	});
	await page.goto('/ui/speak');
	await expect(page.getByRole('main').getByRole('heading').first()).toBeVisible();
	await page.screenshot({
		path: `${out}/desktop-light-speak.png`,
		fullPage: true,
		animations: 'disabled'
	});
	await page.goto('/ui/voices');
	await expect(page.getByRole('main').getByRole('heading').first()).toBeVisible();
	await page.screenshot({
		path: `${out}/desktop-light-voices.png`,
		fullPage: true,
		animations: 'disabled'
	});

	await page.setViewportSize({ width: 390, height: 844 });
	await page.evaluate(() => localStorage.setItem('agent-voice-theme', 'dark'));
	for (const [name, path] of routes) {
		await page.goto(path);
		await expect(page.getByRole('main').getByRole('heading').first()).toBeVisible();
		const overflow = await page.evaluate(
			() => document.documentElement.scrollWidth - window.innerWidth
		);
		expect(overflow, `${name} mobile horizontal overflow`).toBeLessThanOrEqual(1);
		await page.screenshot({
			path: `${out}/mobile-dark-${name}.png`,
			fullPage: true,
			animations: 'disabled'
		});
	}
});
