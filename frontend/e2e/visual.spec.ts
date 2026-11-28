import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const out = '../docs/frontend/screenshots';
const routes = [
	{ name: 'speak', path: '/ui/speak', title: 'Speak' },
	{ name: 'voices', path: '/ui/voices', title: 'Voices' },
	{ name: 'transcribe', path: '/ui/transcribe', title: 'Transcribe' },
	{ name: 'library', path: '/ui/library', title: 'Library' },
	{ name: 'models', path: '/ui/models', title: 'Models' },
	{ name: 'settings', path: '/ui/settings', title: 'Settings' }
] as const;
const viewports = [
	{ width: 1440, height: 900 },
	{ width: 1280, height: 720 },
	{ width: 820, height: 1180 },
	{ width: 390, height: 844 }
] as const;
const themes = ['dark', 'light'] as const;

async function resetData(request: import('@playwright/test').APIRequestContext): Promise<void> {
	for (const [kind, endpoint] of [
		['voice', '/v1/voices'],
		['history', '/v1/history'],
		['capture', '/v1/captures']
	] as const) {
		const response = await request.get(`${endpoint}?limit=200`);
		const payload = (await response.json()) as Record<string, unknown>;
		const items = (payload[kind === 'voice' ? 'voices' : 'items'] ?? []) as {
			id?: string;
			voice_id?: string;
			capture_id?: string;
		}[];
		for (const item of items) {
			const id = item.voice_id ?? item.id ?? item.capture_id;
			if (id) await request.delete(`${endpoint}/${id}`);
		}
	}
}

function seed(): void {
	execFileSync('uv', ['run', 'python', 'scripts/seed_demo.py'], {
		cwd: '..',
		env: { ...process.env, AGENT_VOICE_DEMO_URL: 'http://127.0.0.1:8765' },
		stdio: 'inherit'
	});
}

async function setTheme(
	page: import('@playwright/test').Page,
	mode: 'dark' | 'light'
): Promise<void> {
	await page.goto('/ui/speak');
	await page.evaluate((theme) => localStorage.setItem('agent-voice-theme', theme), mode);
}

async function capture(
	page: import('@playwright/test').Page,
	state: 'empty' | 'populated',
	width: number,
	height: number
): Promise<void> {
	for (const theme of themes) {
		await setTheme(page, theme);
		for (const route of routes) {
			await page.setViewportSize({ width, height });
			await page.goto(route.path);
			await expect(page.getByTestId('top-bar')).toContainText(route.title);
			await page.waitForTimeout(220);
			await page.screenshot({
				path: `${out}/${width}x${height}/${theme}/${route.name}-${state}.png`,
				animations: 'disabled'
			});
		}
	}
}

test.describe.configure({ mode: 'serial' });

test('captures empty and populated screens at every viewport', async ({ page, request }) => {
	test.setTimeout(300_000);
	mkdirSync(out, { recursive: true });
	await resetData(request);
	for (const viewport of viewports) {
		await capture(page, 'empty', viewport.width, viewport.height);
	}

	seed();
	for (const viewport of viewports) {
		await capture(page, 'populated', viewport.width, viewport.height);
	}

	await page.setViewportSize({ width: 1440, height: 900 });
	await setTheme(page, 'dark');
	await page.goto('/ui/voices');
	await page.getByRole('button', { name: 'Clone voice', exact: true }).click();
	const dialog = page.getByRole('dialog', { name: 'Clone a voice' });
	await expect(dialog).toBeVisible();
	await page.screenshot({ path: `${out}/clone-dialog-1-sample.png`, animations: 'disabled' });
	await dialog.locator('#clone-file').setInputFiles({
		name: 'visual-clone.wav',
		mimeType: 'audio/wav',
		buffer: (() => {
			const rate = 16000;
			const seconds = 4;
			const buffer = Buffer.alloc(44 + rate * seconds * 2);
			buffer.write('RIFF', 0);
			buffer.writeUInt32LE(36 + rate * seconds * 2, 4);
			buffer.write('WAVE', 8);
			buffer.write('fmt ', 12);
			buffer.writeUInt32LE(16, 16);
			buffer.writeUInt16LE(1, 20);
			buffer.writeUInt16LE(1, 22);
			buffer.writeUInt32LE(rate, 24);
			buffer.writeUInt32LE(rate * 2, 28);
			buffer.writeUInt16LE(2, 32);
			buffer.writeUInt16LE(16, 34);
			buffer.write('data', 36);
			buffer.writeUInt32LE(rate * seconds * 2, 40);
			return buffer;
		})()
	});
	await dialog.getByRole('button', { name: 'Check and trim', exact: true }).click();
	await expect(dialog.locator('#clone-transcript')).toBeVisible({ timeout: 20_000 });
	await page.screenshot({ path: `${out}/clone-dialog-2-review.png`, animations: 'disabled' });
	await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
	await page.screenshot({ path: `${out}/clone-dialog-3-save.png`, animations: 'disabled' });
});
