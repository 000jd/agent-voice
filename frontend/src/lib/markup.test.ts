import { expect, test } from 'vitest';
import { insertMarkup, MARKUP_ACTIONS } from '#lib/markup.js';

const slow = MARKUP_ACTIONS.find((action) => action.id === 'slow');
const pause = MARKUP_ACTIONS.find((action) => action.id === 'pause');

test('wraps a selection in a paired tag and selects the inner text', () => {
	expect(slow).toBeDefined();
	const result = insertMarkup('hello world', 6, 11, slow!);
	expect(result.text).toBe('hello [slow]world[/slow]');
	expect(result.text.slice(result.selectionStart, result.selectionEnd)).toBe('world');
});

test('inserts an empty pair and leaves the cursor inside', () => {
	const result = insertMarkup('hello', 5, 5, slow!);
	expect(result.text).toBe('hello[slow][/slow]');
	expect(result.selectionStart).toBe(11);
	expect(result.selectionEnd).toBe(11);
});

test('inserts a pause marker at the cursor', () => {
	const result = insertMarkup('hello', 5, 5, pause!);
	expect(result.text).toBe('hello[pause 500ms]');
	expect(result.selectionStart).toBe(result.text.length);
});
