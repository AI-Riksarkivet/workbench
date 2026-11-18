import { describe, expect, it } from 'vitest';
import { keyBelongsElsewhere, type FocusTarget } from './walk-keys.ts';

const el = (tagName: string, inside: string | null = null, editable = false): FocusTarget => ({
	tagName,
	isContentEditable: editable,
	closest: (selector: string) => (inside !== null && selector.includes(inside) ? {} : null),
});

describe('whose key a press is', () => {
	it.each([
		['a select', el('SELECT'), false, true],
		['a text field', el('INPUT'), false, true],
		['an editable line', el('SPAN', null, true), false, true],
		['an option in an open menu', el('DIV', 'role="menu"'), false, true],
		['the canvas, under an open dialog', el('DIV'), true, true],
		['the canvas', el('DIV'), false, false],
		['a button on the side panel', el('BUTTON'), false, false],
	])('on %s belongs elsewhere: %s', (_what, target, open, expected) => {
		expect(keyBelongsElsewhere(target, open)).toBe(expected);
	});
});
