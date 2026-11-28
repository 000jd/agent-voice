import { expect, test } from '@playwright/test';

function wavBuffer(seconds = 4, rate = 16000): Buffer {
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
		buffer.writeInt16LE(
			Math.round(Math.sin((index / rate) * Math.PI * 2 * 440) * 5000),
			44 + index * 2
		);
	}
	return buffer;
}

test('live Studio flow: speak, clone, transcription and library', async ({ page }) => {
	await page.goto('/ui/speak');
	await expect(page.getByText('Connected').first()).toBeVisible();

	// Voice-first preset selection and a real background job.
	await page.getByRole('button', { name: /^Ryan/ }).first().click();
	await page.locator('#script-text').fill('The studio is quiet and the first take is ready.');
	const audioResponse = page.waitForResponse(
		(response) => response.url().includes('/audio/') && response.status() === 200
	);
	await page.getByRole('button', { name: 'Generate speech' }).click();
	await audioResponse;
	await expect(page.getByTestId('player-bar')).toBeVisible();
	await expect(page.getByTestId('player-title')).toContainText('The studio is quiet');

	// A real upload → trim → save flow. No path is typed anywhere.
	await page.getByTestId('rail').getByRole('link', { name: 'Voices', exact: true }).click();
	await page.getByRole('button', { name: 'Clone voice', exact: true }).click();
	const dialog = page.getByRole('dialog', { name: 'Clone a voice' });
	await expect(dialog).toBeVisible();
	await dialog.locator('#clone-file').setInputFiles({
		name: 'studio-clone.wav',
		mimeType: 'audio/wav',
		buffer: wavBuffer()
	});
	await dialog.getByRole('button', { name: 'Check and trim', exact: true }).click();
	await expect(dialog.locator('#clone-transcript')).toBeVisible({ timeout: 20_000 });
	await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
	await dialog.locator('#clone-name').fill(`QA voice ${Date.now()}`);
	await dialog.getByRole('checkbox').last().check();
	await dialog.getByRole('button', { name: 'Save voice', exact: true }).click();
	await expect(dialog.getByText('Voice saved')).toBeVisible({ timeout: 20_000 });
	await dialog.getByRole('button', { name: 'Done', exact: true }).last().click();

	// Transcription upload and server-generated subtitle.
	await page.getByTestId('rail').getByRole('link', { name: 'Transcribe', exact: true }).click();
	await page.locator('#transcribe-file').setInputFiles({
		name: 'studio-transcribe.wav',
		mimeType: 'audio/wav',
		buffer: wavBuffer()
	});
	await expect(page.locator('#transcript-text')).toBeVisible({ timeout: 30_000 });
	await expect(page.getByRole('heading', { name: 'Timed segments' })).toBeVisible();
	const download = page.waitForEvent('download');
	await page.getByRole('button', { name: 'SRT', exact: true }).click();
	expect((await download).suggestedFilename()).toMatch(/\.srt$/);

	// Library survives the page navigation and shows both history and captures.
	await page.getByTestId('rail').getByRole('link', { name: 'Library', exact: true }).click();
	await expect(page.getByRole('heading', { name: 'The library is empty' })).toBeHidden();
	await expect(
		page
			.getByRole('main')
			.getByText(/The studio is quiet|Uploaded audio/)
			.first()
	).toBeVisible();
});

test('records a microphone clip with Chromium fake media', async ({ page }) => {
	await page.goto('/ui/voices');
	await page.getByRole('button', { name: 'Clone voice', exact: true }).click();
	const dialog = page.getByRole('dialog', { name: 'Clone a voice' });
	await dialog.getByRole('button', { name: 'Record', exact: true }).click();
	const stop = dialog.getByRole('button', { name: 'Stop', exact: true });
	await expect(stop).toBeVisible({ timeout: 10_000 });
	await page.waitForTimeout(2_000);
	await stop.click();
	await expect(dialog.getByRole('button', { name: 'Use recording', exact: true })).toBeVisible({
		timeout: 10_000
	});
	await dialog.getByRole('button', { name: 'Use recording', exact: true }).click();
	await dialog.getByRole('button', { name: 'Check and trim', exact: true }).click();
	await expect(dialog.locator('#clone-transcript')).toBeVisible({ timeout: 20_000 });
	await dialog.getByRole('button', { name: 'Close dialog' }).click();
	await expect(dialog).toBeHidden();
});
