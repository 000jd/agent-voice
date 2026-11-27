import { render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { expect, test } from 'vitest';
import { NAV_ITEMS } from '#lib/nav.js';
import AppShell from './app-shell.svelte';

const children = createRawSnippet(() => ({
	render: () => '<p>Shell content</p>'
}));

const nav = NAV_ITEMS.map((item) => ({
	...item,
	href: item.href ? `/${item.href}` : '/'
}));

test('renders the navigation and marks the active route', () => {
	render(AppShell, { nav, currentPath: '/speak', children });

	expect(screen.getByText('Shell content')).toBeInTheDocument();
	expect(screen.getByRole('button', { name: 'Switch to dark theme' })).toBeInTheDocument();

	const speakLinks = screen.getAllByRole('link', { name: 'Speak' });
	expect(speakLinks).toHaveLength(2);
	for (const link of speakLinks) {
		expect(link).toHaveAttribute('aria-current', 'page');
	}

	const settingsLinks = screen.getAllByRole('link', { name: 'Settings' });
	for (const link of settingsLinks) {
		expect(link).not.toHaveAttribute('aria-current');
	}
});
