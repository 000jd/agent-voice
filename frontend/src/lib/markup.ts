export type MarkupAction = {
	id: 'pause' | 'slow' | 'fast' | 'emphasis' | 'spell';
	label: string;
	tag: string | null;
	snippet: string;
};

export const MARKUP_ACTIONS: readonly MarkupAction[] = [
	{ id: 'pause', label: 'Pause', tag: null, snippet: '[pause 500ms]' },
	{ id: 'slow', label: 'Slow', tag: 'slow', snippet: '[slow]' },
	{ id: 'fast', label: 'Fast', tag: 'fast', snippet: '[fast]' },
	{ id: 'emphasis', label: 'Emphasis', tag: 'emphasis', snippet: '[emphasis]' },
	{ id: 'spell', label: 'Spell', tag: 'spell', snippet: '[spell]' }
];

export type MarkupResult = {
	text: string;
	selectionStart: number;
	selectionEnd: number;
};

export function insertMarkup(
	text: string,
	selectionStart: number,
	selectionEnd: number,
	action: MarkupAction
): MarkupResult {
	const start = Math.max(0, Math.min(selectionStart, text.length));
	const end = Math.max(start, Math.min(selectionEnd, text.length));
	const before = text.slice(0, start);
	const selected = text.slice(start, end);
	const after = text.slice(end);

	if (!action.tag) {
		const inserted = action.snippet;
		const cursor = start + inserted.length;
		return {
			text: `${before}${inserted}${selected}${after}`,
			selectionStart: cursor,
			selectionEnd: cursor
		};
	}

	const open = `[${action.tag}]`;
	const close = `[/${action.tag}]`;
	if (selected) {
		const innerStart = start + open.length;
		return {
			text: `${before}${open}${selected}${close}${after}`,
			selectionStart: innerStart,
			selectionEnd: innerStart + selected.length
		};
	}

	const cursor = start + open.length;
	return {
		text: `${before}${open}${close}${after}`,
		selectionStart: cursor,
		selectionEnd: cursor
	};
}
