/**
 * Applying one grid label to many items, and undoing it: ONE item at a time through the annotator's own doors, a few in
 * flight at once, each answered on its own, with the run's progress told as it goes and a stop that leaves the items not
 * yet started as they were.
 *
 * Each item's label is one save of its own work (`POST /api/annotations/<unit>`, `saveWork`): the labels it adds, the
 * tags it removes and the proposals of the label it confirms, stated against the revision the grid read it at, applied
 * in one transaction under that item's row lock with every rule the canvas's own save meets (who may edit it now, its
 * state, a frozen project, a replica's owner, the draft's caps). An item someone may claim is claimed first through the
 * task door (`claim`, the edge its `legal_events` offered), and released again when its label is then refused, since the
 * grid took it only to label it. So nothing here reaches past a per-item check, and an item is either labelled or left as
 * it was: a refusal is that item's, named, and the others go on. An item someone else took meanwhile, or whose claim
 * lapsed, is refused by its door and shown, never overwritten.
 *
 * AN UNDO IS THE SERVER'S. The annotator keeps what undoes each save (`draft_history`), so a label is undone by naming the
 * revision its save wrote (`revertSave`): what it removed comes back whole, with its text, attributes, provenance, scores
 * and links, never rebuilt from what the grid read. Undoing that undo redoes the label the same way.
 */
import { makeInsertRow, type SavePayload } from '@workbench/labeling/annotations-client';
import type { ApiResult } from '#lib/api/client.ts';
import {
	changeFor,
	isEmpty,
	itemTakes,
	labelable,
	type Change,
	type Intent,
} from './label-grid.ts';
import type { TaskDetail } from './types.ts';

/** How many items are written at once: enough to hide a round trip, few enough to leave the annotator's pool to others. */
const IN_FLIGHT = 4;

/** What happened to one item. */
export type Outcome =
	| { status: 'done'; revision: number }
	| { status: 'unchanged' }
	| { status: 'refused'; reason: string }
	| { status: 'not-attempted'; reason: string };

/** One item's label written, as an undo needs it: the revision its save wrote. */
export interface Written {
	task: TaskDetail;
	revision: number;
}

/** The doors a run calls, so the run is the same against the annotator and a test's stand-in. */
export interface Doors {
	claim(task: TaskDetail): Promise<ApiResult<unknown>>;
	release(task: TaskDetail): Promise<ApiResult<unknown>>;
	save(task: TaskDetail, payload: SavePayload): Promise<ApiResult<{ version: number }>>;
	revert(task: TaskDetail, revision: number): Promise<ApiResult<{ version: number }>>;
	/** The item as the annotator answers it now, with its work, or null when it cannot be read. */
	reread(task: TaskDetail): Promise<TaskDetail | null>;
	/** The revision the item's work is at now when it holds what it held before the save that wrote ``revision`` (that
	 *  save is undone), else null. */
	undone(task: TaskDetail, revision: number): Promise<number | null>;
}

/** A refusal that may be a write which landed: no answer came (`NOT CONFIRMED`), or the work had moved, which a resend
 *  of a write that landed meets. The item is read again and judged by what it now holds. */
const mayHaveLanded = (status: number): boolean => status === 0 || status === 409;

/** How a run tells its progress and is stopped: ``progress`` after each item settles, ``stopped`` read before each item
 *  is started (the items in flight finish). */
export interface Control {
	progress(done: number): void;
	stopped(): boolean;
}

const STOPPED = 'stopped before it was started';

/** The save body of ``change`` built on ``revision``: a confirmed proposal is an edit of its status, as the canvas keeps
 *  a proposal. */
function payloadOf(change: Change, revision: number): SavePayload {
	return {
		edits: change.accepts.map((t) => ({ id: t.id, status: 'accepted' })),
		inserts: change.inserts.map((t) =>
			makeInsertRow({ id: t.id, shape_type: 'tag', label: t.label }),
		),
		geometry: [],
		temporal: [],
		spans: [],
		deletes: change.deletes.map((t) => t.id),
		base_version: revision,
	};
}

/** Run ``each`` over ``items`` with at most `IN_FLIGHT` in flight, the answers in the items' order; once ``control``
 *  says stop, the items not yet started answer ``skipped``. */
export async function pooled<T, R>(
	items: readonly T[],
	each: (item: T) => Promise<R>,
	control: Control,
	skipped: R,
): Promise<R[]> {
	const out: R[] = Array.from({ length: items.length }, () => skipped);
	let next = 0;
	let done = 0;
	const worker = async (): Promise<void> => {
		while (next < items.length && !control.stopped()) {
			const at = next++;
			out[at] = await each(items[at] as T);
			control.progress(++done);
		}
	};
	await Promise.all(Array.from({ length: Math.min(IN_FLIGHT, items.length) }, worker));
	return out;
}

/** Apply ``intent`` to each of ``targets`` but those in ``keep`` (items a person chose to keep as they are); each item's
 *  outcome, in order. */
export function applyIntent(
	targets: readonly TaskDetail[],
	intent: Intent,
	grid: ReadonlySet<string>,
	doors: Doors,
	control: Control,
	keep: ReadonlySet<string> = new Set(),
	mint: () => string = () => crypto.randomUUID(),
): Promise<Outcome[]> {
	return pooled(
		targets,
		async (task): Promise<Outcome> => {
			if (keep.has(task.task_id))
				return { status: 'not-attempted', reason: 'kept: what its label carries stays' };
			const may = labelable(task);
			if (may.kind === 'no') return { status: 'refused', reason: may.reason };
			if (intent.kind !== 'clear' && !itemTakes(task, intent.label))
				return {
					status: 'refused',
					reason: `${intent.label} is not one of the labels this item was sent with`,
				};
			const work = task.work ?? { revision: 0, tags: [] };
			const change = changeFor(work.tags, intent, grid, mint);
			if (isEmpty(change)) return { status: 'unchanged' };
			if (may.kind === 'claim-first') {
				const claimed = await doors.claim(task);
				if (!claimed.ok) return { status: 'refused', reason: `not claimed: ${claimed.detail}` };
			}
			const saved = await doors.save(task, payloadOf(change, work.revision));
			if (saved.ok) return { status: 'done', revision: saved.data.version };
			if (mayHaveLanded(saved.status)) {
				// Reported by what the item holds now: labelled as asked (this write landed, or another one did), or refused.
				const fresh = await doors.reread(task);
				if (fresh?.work && isEmpty(changeFor(fresh.work.tags, intent, grid, mint)))
					return { status: 'unchanged' };
			}
			if (may.kind !== 'claim-first') return { status: 'refused', reason: saved.detail };
			// The grid claimed it only to label it: a claim it cannot use goes back.
			const released = await doors.release(task);
			return {
				status: 'refused',
				reason: released.ok
					? `not labelled, and released again: ${saved.detail}`
					: `not labelled: ${saved.detail}; it stays claimed by you (${released.detail})`,
			};
		},
		control,
		{ status: 'not-attempted', reason: STOPPED },
	);
}

/** Undo each of ``written`` on the server (or, given what an undo wrote, redo it): a 409 for an item whose work moved
 *  since, which is left as it is. */
export function revertEach(
	written: readonly Written[],
	doors: Doors,
	control: Control,
): Promise<Outcome[]> {
	return pooled(
		written,
		async ({ task, revision }): Promise<Outcome> => {
			const reverted = await doors.revert(task, revision);
			if (reverted.ok) return { status: 'done', revision: reverted.data.version };
			// An undo whose answer was lost, or resent after it landed: the work holds what it held before that save.
			const now = mayHaveLanded(reverted.status) ? await doors.undone(task, revision) : null;
			return now === null
				? { status: 'refused', reason: reverted.detail }
				: { status: 'done', revision: now };
		},
		control,
		{ status: 'not-attempted', reason: STOPPED },
	);
}
