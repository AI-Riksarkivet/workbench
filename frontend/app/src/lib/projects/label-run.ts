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
 * KEEPING OR REMOVING AN ITEM'S PROPOSALS is one call per item too (`POST /api/annotations/<unit>/proposals`,
 * `settleEach`): the grid holds none of an item's shapes, so the annotator settles every proposal of its work at once, on
 * the revision the grid read, as one save of its own; claimed first and released again on a refusal as a label is.
 *
 * ASKING A MODEL about each item is the canvas's own ask (`POST /api/assist/<unit>`, `askEach`), the item claimed first
 * as a label is, and its answer saved to it as `prediction` rows (`proposal-rows.ts`) on the revision the grid read, as
 * one save of its own: so work that moved since is refused by its door, never overwritten, and the proposals wait for a
 * person to keep or remove them. An ask the model did not answer (too slow, busy, failed), or whose every proposal is a
 * shape the item's classes do not take, is that item's refusal in the annotator's words, and a claim taken for it goes
 * back. The run's signal gives up the asks in flight when the person stops it or leaves the page.
 *
 * AN UNDO IS THE SERVER'S. The annotator keeps what undoes each save (`draft_history`), so a label is undone by naming the
 * revision its save wrote (`revertSave`): what it removed comes back whole, with its text, attributes, provenance, scores
 * and links, never rebuilt from what the grid read. Undoing that undo redoes the label the same way.
 */
import { makeInsertRow, type SavePayload } from '@workbench/labeling/annotations-client';
import type { ApiResult } from '#lib/api/client.ts';
import type { AssistAnswer, AssistAsk, AssistModel } from '#lib/api/assist.api.ts';
import { proposalFields } from '#lib/canvas/proposal-rows.ts';
import {
	changeFor,
	fits,
	isEmpty,
	itemTakes,
	labelable,
	proposedText,
	type Change,
	type Intent,
} from './label-grid.ts';
import type { TaskDetail } from './types.ts';

/** How many items are written at once: enough to hide a round trip, few enough to leave the annotator's pool to others. */
const IN_FLIGHT = 4;

/** Why an item, or a part of what was asked for it, was refused: in the item's own words (``reason``, its cell's), and
 *  in those a summary groups it by (``group``; the reason itself when absent), which say what failed and never which
 *  item it failed for. */
export interface Refusal {
	reason: string;
	group?: string;
}

/** What happened to one item: for an ask, ``proposed`` says what the model's answer put on it and ``left`` what of it
 *  was not kept. */
export type Outcome =
	| { status: 'done'; revision: number | null; proposed?: string; left?: Refusal }
	| { status: 'unchanged' }
	| ({ status: 'refused' } & Refusal)
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
	/** Keep or remove every proposal of the item's work, built on ``revision``. */
	settle(
		task: TaskDetail,
		action: 'keep' | 'remove',
		revision: number,
	): Promise<ApiResult<{ settled: number; version: number }>>;
	revert(task: TaskDetail, revision: number): Promise<ApiResult<{ version: number }>>;
	/** Ask a model about the item's unit, given up when ``signal`` aborts. */
	assist(task: TaskDetail, ask: AssistAsk, signal: AbortSignal): Promise<ApiResult<AssistAnswer>>;
	/** The revision the item's work is at now when it holds every row of ``ids`` (the save that wrote them landed), else
	 *  null. */
	holds(task: TaskDetail, ids: readonly string[]): Promise<number | null>;
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
 *  is started (the items in flight finish), and ``signal``, aborted on a stop and when the page is left, for a call a
 *  run gives up rather than waits for (an ask of a model). */
export interface Control {
	progress(done: number): void;
	stopped(): boolean;
	signal: AbortSignal;
}

const STOPPED = 'stopped before it was started';

/** What ``verb`` and ``why`` say of ``task`` once a claim the grid took only for this call (``claimed``) is given back,
 *  and the same in the words a summary groups it by (``grouped``, else ``why``). */
async function letGo(
	task: TaskDetail,
	claimed: boolean,
	doors: Doors,
	verb: string,
	why: string,
	grouped: string = why,
): Promise<Required<Refusal>> {
	const tail = (words: string): string => (words ? `: ${words}` : '');
	if (!claimed) return { reason: `${verb}${tail(why)}`, group: `${verb}${tail(grouped)}` };
	const released = await doors.release(task);
	return released.ok
		? {
				reason: `${verb}, and released again${tail(why)}`,
				group: `${verb}, and released again${tail(grouped)}`,
			}
		: {
				reason: `${verb}${tail(why)}; it stays claimed by you (${released.detail})`,
				group: `${verb}${tail(grouped)}; it stays claimed by you`,
			};
}

/** The outcome of a write to ``task`` refused with ``failed``: when it may have landed (`mayHaveLanded`), what
 *  ``landed`` finds the item holds now (null: it did not land); else that refusal, in ``verb``'s words where the grid
 *  claimed the item only for this write (``claimed``), since a claim it cannot use goes back. */
async function refusedOr(
	task: TaskDetail,
	failed: { status: number; detail: string },
	claimed: boolean,
	doors: Doors,
	verb: string,
	landed: () => Promise<Outcome | null>,
): Promise<Outcome> {
	if (mayHaveLanded(failed.status)) {
		const now = await landed();
		if (now) return now;
	}
	if (!claimed) return { status: 'refused', reason: failed.detail };
	return {
		status: 'refused',
		reason: (await letGo(task, true, doors, verb, failed.detail)).reason,
	};
}

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
			// Reported by what the item holds now: labelled as asked (this write landed, or another one did), or refused.
			return refusedOr(task, saved, may.kind === 'claim-first', doors, 'not labelled', async () => {
				const fresh = await doors.reread(task);
				return fresh?.work && isEmpty(changeFor(fresh.work.tags, intent, grid, mint))
					? { status: 'unchanged' }
					: null;
			});
		},
		control,
		{ status: 'not-attempted', reason: STOPPED },
	);
}

/** Keep or remove every proposal of each of ``targets``; each item's outcome, in order. An item holding none is
 *  already so. */
export function settleEach(
	targets: readonly TaskDetail[],
	action: 'keep' | 'remove',
	doors: Doors,
	control: Control,
): Promise<Outcome[]> {
	return pooled(
		targets,
		async (task): Promise<Outcome> => {
			const may = labelable(task);
			if (may.kind === 'no') return { status: 'refused', reason: may.reason };
			const work = task.work;
			if (!work || work.proposals === 0) return { status: 'unchanged' };
			if (may.kind === 'claim-first') {
				const claimed = await doors.claim(task);
				if (!claimed.ok) return { status: 'refused', reason: `not claimed: ${claimed.detail}` };
			}
			const settled = await doors.settle(task, action, work.revision);
			if (settled.ok)
				return settled.data.settled === 0
					? { status: 'unchanged' }
					: { status: 'done', revision: settled.data.version };
			// Reported by what the item holds now: no proposal left is what was asked (this call landed, or another did).
			return refusedOr(
				task,
				settled,
				may.kind === 'claim-first',
				doors,
				'not changed',
				async () => {
					const fresh = await doors.reread(task);
					return fresh?.work && fresh.work.proposals === 0 ? { status: 'unchanged' } : null;
				},
			);
		},
		control,
		{ status: 'not-attempted', reason: STOPPED },
	);
}

/** How a summary groups an ask the model did not answer: by what failed (the problem's `type`), never by the words the
 *  endpoint answered for one item, which its cell keeps. */
const UNANSWERED: Readonly<Record<string, string>> = {
	'model-endpoint-timeout': 'the model did not answer in time',
	'model-endpoint-busy': 'the model endpoint was busy',
	'model-endpoint-failed': 'the model endpoint failed',
	'no-model-endpoint': 'the model endpoint is unavailable',
	'model-refused-ask': 'the model refused the ask',
};
/** How a summary groups a proposal the item's classes cannot hold (the annotator's `dropped`). */
const NO_FIT = "a shape this item's classes do not take";

/** Ask ``model`` (as ``ask`` names it) about each of ``targets`` and save its answer to the item as proposals; each
 *  item's outcome, in order. An item whose classes take no shape the model returns is not asked. */
export function askEach(
	targets: readonly TaskDetail[],
	model: Pick<AssistModel, 'returns'>,
	ask: AssistAsk,
	doors: Doors,
	control: Control,
): Promise<Outcome[]> {
	return pooled(
		targets,
		async (task): Promise<Outcome> => {
			if (control.signal.aborted) return { status: 'not-attempted', reason: STOPPED };
			const may = labelable(task);
			if (may.kind === 'no') return { status: 'refused', reason: may.reason };
			const kind = task.media?.kind ?? 'image';
			if (kind !== 'image')
				return {
					status: 'refused',
					reason: `a model is shown a picture, and this item is ${kind}`,
				};
			if (!fits(model, task))
				return {
					status: 'refused',
					reason: `it returns ${model.returns.join(', ') || 'nothing named'}, no shape this item's classes take`,
					group: NO_FIT,
				};
			const claimed = may.kind === 'claim-first';
			if (claimed) {
				const took = await doors.claim(task);
				if (!took.ok) return { status: 'refused', reason: `not claimed: ${took.detail}` };
			}
			const answered = await doors.assist(task, ask, control.signal);
			if (control.signal.aborted)
				return {
					status: 'not-attempted',
					reason: (await letGo(task, claimed, doors, 'stopped while it was asked', '')).reason,
				};
			if (!answered.ok) {
				const what =
					(answered.type && UNANSWERED[answered.type]) ||
					(answered.status === 0 ? 'no answer came' : answered.detail);
				return {
					status: 'refused',
					...(await letGo(task, claimed, doors, 'not answered', answered.detail, what)),
				};
			}
			const { proposals, dropped } = answered.data;
			if (proposals.length === 0 && dropped.length > 0)
				return {
					status: 'refused',
					...(await letGo(
						task,
						claimed,
						doors,
						'nothing it proposed fits',
						dropped.join('; '),
						NO_FIT,
					)),
				};
			if (proposals.length === 0) {
				if (!claimed) return { status: 'unchanged' };
				const released = await doors.release(task);
				return released.ok
					? { status: 'unchanged' }
					: {
							status: 'refused',
							reason: `it found nothing; it stays claimed by you (${released.detail})`,
						};
			}
			const base = (task.work ?? { revision: 0 }).revision;
			const inserts = proposals.map((p) => makeInsertRow(proposalFields(answered.data, p)));
			const saved = await doors.save(task, {
				edits: [],
				inserts,
				geometry: [],
				temporal: [],
				spans: [],
				deletes: [],
				base_version: base,
			});
			// What of the answer the item's classes could not hold is named on it beside what it kept.
			const left =
				dropped.length > 0 ? { left: { reason: dropped.join('; '), group: NO_FIT } } : {};
			const done = (revision: number | null): Outcome => ({
				status: 'done',
				revision,
				proposed: proposedText(answered.data),
				...left,
			});
			if (saved.ok) return done(saved.data.version);
			// Reported by what the item holds now: the rows this save wrote (it landed), or refused.
			return refusedOr(task, saved, claimed, doors, 'not saved', async () => {
				const at = await doors.holds(
					task,
					inserts.map((r) => r.id),
				);
				// The save from `base` wrote `base + 1`: at a later revision another save landed on top, and an undo
				// naming that one would take it back, so the item is reported saved with no undo of its own.
				return at === null ? null : done(at === base + 1 ? at : null);
			});
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
