import { fireEvent, render, screen } from '@testing-library/svelte';
import { expect, test, vi } from 'vitest';
import Composer from './composer.svelte';

test('counts characters, inserts markup, and generates with the button or shortcut', async () => {
	const onGenerate = vi.fn();
	render(Composer, { onGenerate });
	const textarea = screen.getByLabelText('Speech text');

	await fireEvent.input(textarea, { target: { value: 'Hello' } });
	expect(screen.getByText('5 / 20,000')).toBeInTheDocument();

	await fireEvent.click(screen.getByRole('button', { name: '[slow]' }));
	expect((textarea as HTMLTextAreaElement).value).toContain('[slow]');
	expect((textarea as HTMLTextAreaElement).value).toContain('[/slow]');

	await fireEvent.click(screen.getByRole('button', { name: 'Generate speech' }));
	expect(onGenerate).toHaveBeenCalledTimes(1);

	await fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true });
	expect(onGenerate).toHaveBeenCalledTimes(2);
});
