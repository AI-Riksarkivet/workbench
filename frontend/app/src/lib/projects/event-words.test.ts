import { describe, expect, it } from 'vitest';

import {
	eventWording,
	labelWording,
	markerText,
	stepWording,
	told,
	verdictDone,
} from './event-words.ts';

describe('what an action is called', () => {
	it('tells a run in the present and a finished action in the past', () => {
		expect(eventWording('request_changes')).toEqual({ running: 'Sending back', done: 'Sent back' });
	});

	it('calls an event it has no words for by its button', () => {
		expect(eventWording('teleport')).toEqual({ running: 'teleport', done: 'teleport' });
	});

	it('words a review’s verdict as its event, and nothing for what is no verdict', () => {
		expect([verdictDone('accepted'), verdictDone('fix_and_accept'), verdictDone(null)]).toEqual([
			'Accepted',
			'Accepted with fixes',
			null,
		]);
	});

	it('words each kind of grid label', () => {
		expect(
			[
				labelWording({ kind: 'set', label: 'map' }),
				labelWording({ kind: 'toggle', label: 'map' }),
				labelWording({ kind: 'clear' }),
			].map((w) => w.running),
		).toEqual(['Labelling map', 'Toggling map', 'Clearing the labels']);
	});

	it('tells an undo and a redo by the step of the history they take', () => {
		const asked = { running: 'Asking a model', done: 'Asked a model' };
		expect([
			stepWording('undo', 2, 2, asked).done,
			stepWording('redo', 1, 2, asked).running,
		]).toEqual(['Undone step 2 of 2 (Asked a model)', 'Redoing step 1 of 2 (Asked a model)']);
	});

	it('names the item an action was done to, or counts the items', () => {
		expect([told('Claimed', 'letter-001'), told('Claimed', 1), told('Claimed', 3)]).toEqual([
			'Claimed: letter-001.',
			'Claimed: 1 item.',
			'Claimed: 3 items.',
		]);
	});

	it('says a cell’s marker in one clause, whatever left the item out', () => {
		expect([
			markerText('refused', 'not claimed'),
			markerText('not-attempted', 'stopped'),
			markerText('left-out', '1 polygon proposal(s)'),
		]).toEqual([
			'refused, not claimed',
			'not attempted, stopped',
			'left out 1 polygon proposal(s)',
		]);
	});
});
