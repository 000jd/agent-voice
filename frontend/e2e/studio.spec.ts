import { mkdirSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const out = '/tmp/agent-voice-qa';

function wavBuffer(seconds = 1, rate = 16000): Buffer {
	const samples = Math.floor(seconds * rate);
	const buffer = Buffer.alloc(44 + samples * 2);
	buffer.write('RIFF', 0);
	buffer.writeUInt32LE(36 + samples * 2, 4);
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
	buffer.writeUInt32LE(samples * 2, 40);
	for (let index = 0; index < samples; index += 1) {
		const value = Math.round(Math.sin((index / rate) * Math.PI * 2 * 440) * 5000);
		buffer.writeInt16LE(value, 44 + index * 2);
	}
	return buffer;
}

test('live studio flow', async ({ page }) => {
	mkdirSync(out, { recursive: true });
	await page.goto('/ui/');
	await expect(page.getByText(/daemon connected/i)).toBeVisible();

	await page.goto('/ui/speak');
	await page.locator('#speak-engine').selectOption('mock');
	await page.getByRole('button', { name: 'Preset', exact: true }).click();
	await page.locator('#speak-model').selectOption('mock-custom');
	await page.locator('#speak-speaker').selectOption('Ryan');
	await page.getByLabel('Speech text').fill('Studio check.');
	const audioResponse = page.waitForResponse(
		(response) => response.url().includes('/audio/') && response.status() === 200
	);
	await page.getByRole('button', { name: 'Generate speech' }).click();
	await audioResponse;
	await expect(page.getByTestId('player-bar')).toBeVisible();
	await expect(page.getByTestId('player-title')).toContainText('Studio check.');
	await expect(page.getByTestId('player-bar')).not.toContainText('protected by the daemon API key');
	await expect
		.poll(async () => page.getByTestId('player-time').textContent())
		.not.toContain('0:00.00 /');

	await page.locator('aside').getByRole('link', { name: 'Voices', exact: true }).click();
	await expect(page.getByRole('main').getByRole('heading', { name: 'Voices' })).toBeVisible();
	await page.getByRole('button', { name: 'Design voice' }).click();
	const drawer = page.getByRole('dialog', { name: 'Design a voice' });
	await expect(drawer).toBeVisible();
	const voiceName = `qa-designed-${Date.now()}`;
	await drawer.getByLabel('Voice name').fill(voiceName);
	await drawer.getByLabel('Description').fill('quiet, precise, late-night studio narration');
	await drawer.getByLabel('Engine').selectOption('mock');
	await drawer.getByLabel('Model').selectOption('mock-design');
	await drawer.getByRole('button', { name: 'Audition takes' }).click();
	await expect(drawer.getByText('Take 1')).toBeVisible({ timeout: 20_000 });
	await expect(drawer.getByText('Take 2')).toBeVisible();
	await drawer.getByRole('button', { name: 'Play take 1' }).click();
	await expect(page.getByTestId('player-bar')).toBeVisible();
	await page.screenshot({ path: `${out}/desktop-dark-design-drawer.png`, animations: 'disabled' });
	await drawer.locator('button[aria-pressed]').first().click();
	await drawer.getByRole('button', { name: 'Save selected take' }).click();
	await expect(drawer).toBeHidden({ timeout: 20_000 });
	await page.getByRole('button', { name: /Designed/ }).click();
	await expect(page.getByText(voiceName, { exact: true })).toBeVisible();
	await page.getByRole('button', { name: 'Use in Speak' }).first().click();
	await expect(page).toHaveURL(/\/ui\/speak$/);
	await expect(page.getByRole('button', { name: 'Clone', exact: true })).toHaveAttribute(
		'aria-pressed',
		'true'
	);

	await page.locator('aside').getByRole('link', { name: 'Transcribe', exact: true }).click();
	await expect(page.getByRole('main').getByRole('heading', { name: 'Transcribe' })).toBeVisible();
	await page.locator('#transcribe-file').setInputFiles({
		name: 'studio-check.wav',
		mimeType: 'audio/wav',
		buffer: wavBuffer()
	});
	await page.locator('#transcribe-engine').selectOption('mock_asr');
	await page.locator('#transcribe-model').selectOption('mock-asr-parity');
	const transcribeResponse = page.waitForResponse(
		(response) => response.url().includes('/v1/audio/transcriptions') && response.status() === 200
	);
	await page.getByRole('button', { name: 'Transcribe', exact: true }).click();
	await transcribeResponse;
	await expect(page.getByRole('heading', { name: 'Transcript' })).toBeVisible();
	await expect(page.getByRole('heading', { name: 'Timed segments' })).toBeVisible();
	await page.screenshot({
		path: `${out}/desktop-dark-transcribe-populated.png`,
		animations: 'disabled'
	});
	const download = page.waitForEvent('download');
	await page.getByRole('button', { name: 'Download .srt' }).click();
	expect((await download).suggestedFilename()).toMatch(/\.srt$/);

	await page.locator('aside').getByRole('link', { name: 'Models & Engines', exact: true }).click();
	await expect(
		page.getByRole('main').getByRole('heading', { name: 'Models & Engines' })
	).toBeVisible();
	await expect(page.getByText('Mock (tone generator, no dependencies)')).toBeVisible();
	await expect(page.getByText('Mock ASR', { exact: false })).toBeVisible();

	await page.locator('aside').getByRole('link', { name: 'Dashboard', exact: true }).click();
	await expect(page.getByText('Studio check.')).toBeVisible();
});
