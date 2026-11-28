import { render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { expect, test } from 'vitest';
import { NAV_GROUPS } from '#lib/nav.js';
import AppShell from './app-shell.svelte';

const children = createRawSnippet(() => ({
	render: () => '<p>Shell content</p>'
}));

const groups = NAV_GROUPS.map((group) => ({
	...group,
	items: group.items.map((item) => ({
		...item,
		href: item.href ? `/ui/${item.href}` : '/ui'
	}))
}));

test('renders the grouped navigation and marks the active route', () => {
	render(AppShell, { groups, currentPath: '/ui/speak', children });

	expect(screen.getByText('Shell content')).toBeInTheDocument();
	expect(screen.getByRole('button', { name: /Switch to (light|dark) theme/ })).toBeInTheDocument();
	expect(screen.getAllByText('Studio').length).toBeGreaterThan(0);
	expect(screen.getAllByText('Library').length).toBeGreaterThan(0);

	const speakLinks = screen.getAllByRole('link', { name: 'Speak' });
	expect(speakLinks).toHaveLength(1);
	expect(speakLinks[0]).toHaveAttribute('aria-current', 'page');

	const settingsLinks = screen.getAllByRole('link', { name: 'Settings' });
	expect(settingsLinks[0]).not.toHaveAttribute('aria-current');
});
