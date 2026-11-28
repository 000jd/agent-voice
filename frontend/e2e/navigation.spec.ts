import { expect, test } from '@playwright/test';

const routes = [
	{ label: 'Speak', heading: 'Speak' },
	{ label: 'Voices', heading: 'Voices' },
	{ label: 'Transcribe', heading: 'Transcribe' },
	{ label: 'Library', heading: 'Library' },
	{ label: 'Models', heading: 'Models' },
	{ label: 'Settings', heading: 'Settings' }
];

test('one rail navigates every Studio screen', async ({ page }) => {
	await page.goto('/ui/');
	await expect(page).toHaveURL(/\/ui\/speak$/);
	await expect(page.getByTestId('top-bar').getByRole('heading', { name: 'Speak' })).toBeVisible();

	for (const route of routes) {
		await page.getByTestId('rail').getByRole('link', { name: route.label, exact: true }).click();
		await expect(
			page.getByTestId('top-bar').getByRole('heading', { name: route.heading })
		).toBeVisible();
	}
});

test('theme defaults dark and survives reload', async ({ page }) => {
	await page.goto('/ui/speak');
	const html = page.locator('html');
	await expect(html).toHaveClass(/dark/);
	await page.getByRole('button', { name: 'Switch to light theme' }).click();
	await expect(html).not.toHaveClass(/dark/);
	await page.reload();
	await expect(html).not.toHaveClass(/dark/);
});

test('command palette navigates with the keyboard', async ({ page }) => {
	await page.goto('/ui/speak');
	await expect(page.getByTestId('top-bar')).toBeVisible();
	await page.keyboard.press('Control+K');
	const palette = page.getByRole('dialog', { name: 'Command palette' });
	await expect(palette).toBeVisible();
	await palette.getByRole('combobox', { name: 'Search commands and voices' }).fill('Go to Voices');
	await page.keyboard.press('Enter');
	await expect(page).toHaveURL(/\/ui\/voices$/);
});

test('help is available without leaving the Studio', async ({ page }) => {
	await page.goto('/ui/speak');
	await page.getByRole('button', { name: 'Keyboard shortcuts and privacy' }).click();
	await expect(page.getByRole('dialog', { name: 'Shortcuts and privacy' })).toBeVisible();
});
