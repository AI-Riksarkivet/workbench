/**
 * What an action on items is called while it runs and once it is done: the one set of words the Items tab (its row
 * actions and its bulk actions), the Label grid and a review pass's verdict read, so one action reads the same wherever
 * it is offered. A run in progress is told in the present ("Claiming"), a finished action in the past ("Claimed"),
 * naming the item it was done to, or how many items for an action over many.
 */
import type { Intent } from './label-grid.ts';
import { EVENT_LABELS } from './presentation.ts';

/** An action's words: while it runs, and once it is done. */
export interface Wording {
	running: string;
	done: string;
}

/** Every event an item's `legal_events` offer from a table, a selection or the canvas's panel. */
const EVENTS: Record<string, Wording> = {
	claim: { running: 'Claiming', done: 'Claimed' },
	release: { running: 'Releasing', done: 'Released' },
	submit: { running: 'Submitting for review', done: 'Submitted for review' },
	skip: { running: 'Skipping', done: 'Skipped' },
	requeue: { running: 'Requeuing', done: 'Requeued' },
	reopen: { running: 'Reopening', done: 'Reopened' },
	accept: { running: 'Accepting', done: 'Accepted' },
	fix_and_accept: { running: 'Accepting with fixes', done: 'Accepted with fixes' },
	request_changes: { running: 'Sending back', done: 'Sent back' },
};

/** The words of ``event``; an event the task machine grows that has no words here is called by its button's. */
export function eventWording(event: string): Wording {
	const known = EVENTS[event];
	if (known) return known;
	const label = (EVENT_LABELS[event] ?? event).replace('…', '');
	return { running: label, done: label };
}

/** The words of an assignment to ``to`` (a person's name): the one event that carries an argument a table names. */
export const assignWording = (to: string): Wording => ({
	running: `Assigning to ${to}`,
	done: `Assigned to ${to}`,
});

/** A review's verdict (`review_action`) as its event's done words, or null for one that is no verdict. */
export function verdictDone(action: string | null | undefined): string | null {
	const event = action === 'accepted' ? 'accept' : action;
	return event === 'accept' || event === 'fix_and_accept' || event === 'request_changes'
		? eventWording(event).done
		: null;
}

/** The words of a grid label. */
export function labelWording(intent: Intent): Wording {
	if (intent.kind === 'clear') return { running: 'Clearing the labels', done: 'Cleared' };
	if (intent.kind === 'set')
		return { running: `Labelling ${intent.label}`, done: `Labelled ${intent.label}` };
	return { running: `Toggling ${intent.label}`, done: `Toggled ${intent.label}` };
}

/** The words of keeping or removing an item's proposals. */
export const settleWording = (action: 'keep' | 'remove'): Wording =>
	action === 'keep'
		? { running: 'Keeping the proposals', done: 'Kept the proposals' }
		: { running: 'Removing the proposals', done: 'Removed the proposals' };

/** The words of asking the model called ``model``. */
export const askWording = (model: string): Wording => ({
	running: `Asking ${model}`,
	done: `Asked ${model}`,
});

/** The words of undoing or redoing a step of the grid's history: which step it is of how many the history holds, so
 *  two steps of the same kind taken back one after the other are told apart, and what the step was. */
export function stepWording(
	verb: 'undo' | 'redo',
	step: number,
	of: number,
	what: Wording,
): Wording {
	const which = `step ${step} of ${of} (${what.done})`;
	return verb === 'undo'
		? { running: `Undoing ${which}`, done: `Undone ${which}` }
		: { running: `Redoing ${which}`, done: `Redone ${which}` };
}

/** A done action told as a sentence: naming the item (``subject`` a name) or counting the items (a number). */
export function told(done: string, subject: string | number): string {
	return typeof subject === 'number'
		? `${done}: ${subject} item${subject === 1 ? '' : 's'}.`
		: `${done}: ${subject}.`;
}

/** What a grid cell's marker says of an item an action left out: refused, not attempted, or part of an answer left
 *  out, each with its reason, in one clause. The visible marker and the cell's accessible name read the same words. */
export function markerText(kind: 'refused' | 'not-attempted' | 'left-out', reason: string): string {
	if (kind === 'left-out') return `left out ${reason}`;
	return `${kind === 'refused' ? 'refused' : 'not attempted'}, ${reason}`;
}
