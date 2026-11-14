/**
 * What a project receives from a selection. The rule that matters most is the partial-selection refusal: the atlas lists
 * a selection's rows capped while it reports the true size, and sending the listed half reports success.
 */
import { describe, expect, it } from 'vitest';
import { predictionFor, SEND_TASK_CAP, selectionKeys, sendItemsFor, sendRefusal } from './send.ts';

describe('the label a selection gets', () => {
	it('is one whole-item tag, trimmed, with no source', () => {
		expect(predictionFor(' letter ')).toEqual([{ shape_type: 'tag', label: 'letter' }]);
	});

	it('is absent when no label was chosen', () => {
		expect(predictionFor('   ')).toBeUndefined();
	});
});

describe('when a selection cannot be sent', () => {
	it('refuses an empty selection', () => {
		expect(sendRefusal({ fetched: 0, total: 0 })).toMatch(/to have items to send/);
	});

	it('refuses a fully loaded selection over the cap, naming the cap', () => {
		const reason = sendRefusal({ fetched: 1500, total: 1500 });

		expect(reason).toBe(
			`1,500 items selected exceeds the ${SEND_TASK_CAP.toLocaleString()} one send takes: narrow the selection.`,
		);
	});

	it('refuses when fewer items have loaded than are selected', () => {
		expect(sendRefusal({ fetched: 400, total: 900 })).toBe(
			'Only 400 of 900 selected items have loaded: narrow the selection so all of them can be sent.',
		);
	});

	it('refuses with no project chosen', () => {
		expect(sendRefusal({ fetched: 5, total: 5, projectChosen: false })).toBe(
			'Choose a project to send to.',
		);
	});

	it('allows exactly the cap, fully loaded', () => {
		expect(sendRefusal({ fetched: SEND_TASK_CAP, total: SEND_TASK_CAP })).toBeNull();
	});
});

describe('the keys and items a selection produces', () => {
	const keyPath = (h: { doc: string; n: number }) => [encodeURIComponent(h.doc), String(h.n)];

	it('joins the caller descriptor key path, de-duplicated, blank ones dropped', () => {
		const rows = [
			{ doc: 'a b', n: 1 },
			{ doc: 'a b', n: 1 },
			{ doc: 'd1', n: 3 },
		];

		expect(selectionKeys(rows, keyPath)).toEqual(['a%20b/1', 'd1/3']);
		expect(selectionKeys([{ doc: '', n: 0 }], () => [])).toEqual([]);
	});

	it('sends each key from its dataset at its version, with the label when one was chosen', () => {
		const from = { dataset: 'acme$letters', version: 7, mediaKind: 'none' } as const;

		expect(sendItemsFor(['d1/3'], from, 'letter')).toEqual([
			{
				source: { kind: 'chunks', keys: ['d1/3'], where: 'acme$letters', dataset_version: 7 },
				media: { kind: 'text' },
				prediction: [{ shape_type: 'tag', label: 'letter' }],
			},
		]);
	});
});
