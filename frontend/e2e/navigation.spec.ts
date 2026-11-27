import { expect, test } from '@playwright/test';

const routes = [
	{ label: 'Speak', heading: 'Speak' },
	{ label: 'Voices', heading: 'Voices' },
	{ label: 'Transcribe', heading: 'Transcribe' },
	{ label: 'Models', heading: 'Models' },
	{ label: 'History', heading: 'History' },
	{ label: 'Settings', heading: 'Settings' }
];

test('navigates every stub route from the sidebar', async ({ page }) => {
	await page.goto('/ui/');
	await expect(page.getByRole('heading', { name: 'Dashboard', exact: true })).toBeVisible();

	for (const route of routes) {
		await page.locator('aside').getByRole('link', { name: route.label, exact: true }).click();
		await expect(page.getByRole('heading', { name: route.heading, exact: true })).toBeVisible();
	}
});

test('toggles the colour theme and persists it across reloads', async ({ page }) => {
	await page.emulateMedia({ colorScheme: 'light' });
	await page.goto('/ui/');

	const html = page.locator('html');
	await expect(html).not.toHaveClass(/dark/);

	await page.getByRole('button', { name: 'Switch to dark theme' }).click();
	await expect(html).toHaveClass(/dark/);

	await page.reload();
	await expect(html).toHaveClass(/dark/);
});

test('shows the dashboard error state when the daemon is unreachable', async ({ page }) => {
	await page.route('**/health', (route) => route.abort());
	await page.route('**/v1/engines', (route) => route.abort());

	await page.goto('/ui/');
	await expect(page.getByText('Daemon not reachable')).toBeVisible();
	await expect(page.getByText('agent-voice serve', { exact: true })).toBeVisible();
});
