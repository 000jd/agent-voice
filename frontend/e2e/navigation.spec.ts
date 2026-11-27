import { expect, test } from '@playwright/test';

const routes = [
	{ label: 'Speak', heading: 'Speak' },
	{ label: 'Voices', heading: 'Voices' },
	{ label: 'Transcribe', heading: 'Transcribe' },
	{ label: 'Models & Engines', heading: 'Models & Engines' },
	{ label: 'History', heading: 'History' },
	{ label: 'Settings', heading: 'Settings' }
];

test('navigates every studio route from the sidebar', async ({ page }) => {
	await page.goto('/ui/');
	const main = page.getByRole('main');
	await expect(main.getByRole('heading', { name: 'Dashboard', exact: true })).toBeVisible();

	for (const route of routes) {
		await page.locator('aside').getByRole('link', { name: route.label, exact: true }).click();
		await expect(main.getByRole('heading', { name: route.heading, exact: true })).toBeVisible();
	}
});

test('starts dark and toggles to light across reloads', async ({ page }) => {
	await page.goto('/ui/');

	const html = page.locator('html');
	await expect(html).toHaveClass(/dark/);

	await page.getByRole('button', { name: 'Switch to light theme' }).click();
	await expect(html).not.toHaveClass(/dark/);

	await page.reload();
	await expect(html).not.toHaveClass(/dark/);
});

test('shows the dashboard offline state when the daemon is unreachable', async ({ page }) => {
	await page.route('**/health', (route) => route.abort());
	await page.route('**/v1/engines', (route) => route.abort());

	await page.goto('/ui/');
	await expect(page.getByText(/daemon offline/i)).toBeVisible();
});

test('collapses the rail and remembers it', async ({ page }) => {
	await page.goto('/ui/');
	const aside = page.locator('aside');
	await page.getByRole('button', { name: 'Collapse navigation rail' }).click();
	await expect(aside).toHaveClass(/w-16/);
	await page.reload();
	await expect(aside).toHaveClass(/w-16/);
	await page.getByRole('button', { name: 'Expand navigation rail' }).click();
	await expect(aside).toHaveClass(/w-\[220px\]/);
});
