/**
 * Applying one grid label to many items, and undoing it: ONE item at a time through the annotator's own doors, a few in
 * flight at once, each answered on its own, with the run's progress told as it goes and a stop that leaves the items not
 * yet started as they were.
 *
 * Each item's label is one save of its own work (`POST /api/annotations/<unit>`, `saveWork`): the labels it adds, the
 * tags it removes and the proposals of the label it confirms, stated against the revision the grid read it at, applied
 * in one transaction under that item's row lock with every rule the canvas's own save meets (who may edit it now, its
 * state, a frozen project, a replica's owner, the draft's caps). An item someone may claim is claimed first through the
 * task door (`claim`, the edge its `legal_events` offered), and released again when its label is then refused (where it
 * was open), since the grid took it only to label it. So nothing here reaches past a per-item check, and an item is either labelled or left as
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
 * person to keep or remove them. Asked again, a model's answer replaces the proposals of that model the item still holds
 * in the same save (read from the item's rows, since the listing names no row's model), as the canvas's refine retracts
 * the session's own, so one undo takes the whole replacement back; another model's proposals and every row a person
 * kept or made stay. An ask the model did not answer (too slow, busy, failed), or whose every proposal is a
 * shape the item's classes do not take, is that item's refusal in the annotator's words, and a claim taken for it goes
 * back. The run's signal gives up the asks in flight when the person stops it or leaves the page.
 *
 * AN UNDO IS THE SERVER'S. The annotator keeps what undoes each save (`draft_history`), so a label is undone by naming the
 * revision its save wrote (`revertSave`): what it removed comes back whole, with its text, attributes, provenance, scores
 * and links, never rebuilt from what the grid read. Undoing that undo redoes the label the same way. The annotator takes
 * an undo while the work is as that save left it, at its revision or with every save since taken back, so the grid's
 * stack is walked back one run after another and forward again, each entry naming its own save's revision. A step it
 * refuses (someone else changed the item since) is named on the item and leaves the stack; the steps after it stay.
 *
 * A CLAIM THE RUN TOOK goes back only where the item was open (`unassigned`) before it, as the claim's own answer says
 * (the `from` of its trail's last claim, `claimOrigin`), never the listing read earlier, which the item may have moved
 * on from: on a refusal, when what was asked turns out already so, and with the run's undo, and its redo takes it again.
 * An item that had been sent back (`changes_requested`), or whose answer does not say, keeps the claim, said on it: a
 * released item is open and no edge sends it back again, so releasing it would take it out of its annotator's sent-back
 * work for good. A refused write that may have landed is judged by what the item holds: its own minted rows there, it
 * landed, done with its claim and an undo; what was asked already so otherwise, unchanged, its claim handled as for a
 * refusal. An undo releases only the claim the run took: the run keeps what tells that claim apart, its holder and the
 * instant the claim's own answer says it was taken (`claimMark`), and the undo reads the item once before releasing. A
 * claim the person let go and took again since, anywhere (another tab, the canvas, a lapsed lease claimed again, a
 * manager's release and a new claim), names a later instant and is theirs, left with nothing said. The lease's expiry
 * cannot tell the two apart: every save into a claim renews it, another tab's too. A claim also stays where that read or
 * its release fails, said on the item, and once the person fires a transition on the item from the grid its claim is
 * theirs.
 */
import {
	makeInsertRow,
	type InsertRow,
	type SavePayload,
} from '@workbench/labeling/annotations-client';
import type { ApiResult } from '#lib/api/client.ts';
import type { AssistAnswer, AssistAsk, AssistModel } from '#lib/api/assist.api.ts';
import { modelSource, proposalFields } from '#lib/canvas/proposal-rows.ts';
import {
	changeFor,
	claimMark,
	claimOrigin,
	fits,
	isEmpty,
	itemTakes,
	labelable,
	shapesProposed,
	type Change,
	type Intent,
} from './label-grid.ts';
import type { TaskDetail } from './types.ts';

/** How many items are written at once: enough to hide a round trip, few enough to leave the annotator's pool to others. */
export const IN_FLIGHT = 4;

/** Why an item, or a part of what was asked for it, was refused: in the item's own words (``reason``, its cell's), and
 *  in those a summary groups it by (``group``; the reason itself when absent), which say what failed and never which
 *  item it failed for. */
export interface Refusal {
	reason: string;
	group?: string;
}

/** A claim a run took to write an item, both read from the claim's own answer: what the item was before (``from``,
 *  `claimOrigin`), open (`unassigned`), a claim given back and taken again by a redo, sent back (`changes_requested`), a
 *  claim kept, since releasing it would make the item open rather than sent back, or unknown (the answer's trail does
 *  not say), a claim kept too; and what tells that claim from a later one (``mark``, `claimMark`; null when the answer
 *  named none, and then no undo releases it). */
export interface ClaimTaken {
	from: 'open' | 'sent-back' | 'unknown';
	mark: string | null;
}

/** What happened to one item: ``claimed`` when the run took the item's claim to write it, and ``stays`` why a claim it
 *  would have given back stays the reader's; for an ask, ``proposed`` says what the model's answer put on it beyond the
 *  whole-item tags its listing says, and ``left`` what of it was not kept. An item left as it was may say why
 *  (``note``). */
export type Outcome =
	| {
			status: 'done';
			revision: number | null;
			claimed?: ClaimTaken;
			stays?: Refusal;
			proposed?: string;
			left?: Refusal;
	  }
	| { status: 'unchanged'; note?: string; stays?: Refusal }
	| ({ status: 'refused' } & Refusal)
	| { status: 'not-attempted'; reason: string };

type Done = Extract<Outcome, { status: 'done' }>;
type Refused = Extract<Outcome, { status: 'refused' }>;

/** One item's label written, as an undo needs it: the revision its save wrote, and the claim the run that wrote it took
 *  to (``claimed``; null when it took none, or the reader has fired a transition on the item since, which makes the
 *  claim theirs). */
export interface Written {
	task: TaskDetail;
	revision: number;
	claimed: ClaimTaken | null;
}

/** Why a claim stays the reader's when the item could not be read to tell whether it is still the run's own. */
const CLAIM_UNREAD =
	'it stays claimed by you: the item could not be read to tell whether the claim is still the one this run took';

/** Why a claim taken on an item that had been sent back stays the reader's. */
const SENT_BACK_STAYS =
	'it stays claimed by you: it had been sent back, and releasing it would not send it back again';

/** Why a claim stays the reader's when its answer did not say what the item was before. */
const ORIGIN_UNKNOWN_STAYS =
	"it stays claimed by you: the claim's answer did not say whether it had been sent back";

/** Why a claim the run keeps, taken on an item that was not open, stays the reader's. */
const KEPT = {
	'sent-back': SENT_BACK_STAYS,
	unknown: ORIGIN_UNKNOWN_STAYS,
} as const satisfies Record<Exclude<ClaimTaken['from'], 'open'>, string>;

/** The doors a run calls, so the run is the same against the annotator and a test's stand-in. */
export interface Doors {
	/** Claim the item; answers the item as the claim left it. */
	claim(task: TaskDetail): Promise<ApiResult<TaskDetail>>;
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
	/** The ids of the proposals ``source`` (`modelSource`) made on the item's work that nobody kept or removed, read
	 *  until ``signal`` aborts. */
	proposals(
		task: TaskDetail,
		source: string,
		signal: AbortSignal,
	): Promise<ApiResult<{ ids: string[] }>>;
	/** The revision the item's work is at now when it holds every row of ``ids`` (the save that wrote them landed), else
	 *  null. */
	revisionHolding(task: TaskDetail, ids: readonly string[]): Promise<number | null>;
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

/** What a run goes through, and how it is told and stopped. */
export interface Run {
	doors: Doors;
	control: Control;
}

const NOT_STARTED: Outcome = { status: 'not-attempted', reason: 'stopped before it was started' };

/** A step of one item's write: what it gives the next step, or the item's outcome, which ends its write. */
type Step<T> = { ok: true; value: T } | { ok: false; outcome: Outcome };

/** One item's write as a run makes it: the item, the doors it goes through, and the claim the run took for this write
 *  (``claimed``, null for none), which goes back when the write cannot use it and the item was open. */
interface ItemWrite {
	task: TaskDetail;
	doors: Doors;
	claimed: ClaimTaken | null;
}

/** The item ``task`` taken for a write: as it is where the reader may edit it now, claimed first where it is open. */
async function taken(
	task: TaskDetail,
	may: 'edit' | 'claim-first',
	doors: Doors,
): Promise<Step<ItemWrite>> {
	if (may === 'edit') return { ok: true, value: { task, doors, claimed: null } };
	const took = await doors.claim(task);
	if (!took.ok)
		return { ok: false, outcome: { status: 'refused', reason: `not claimed: ${took.detail}` } };
	const from = claimOrigin(took.data) ?? 'unknown';
	return { ok: true, value: { task, doors, claimed: { from, mark: claimMark(took.data) } } };
}

/** A refusal's words: what was not done (``verb``), why in the item's own words (``why``), and why in those a summary
 *  groups it by (``grouped``; ``why`` when absent). */
interface Said {
	verb: string;
	why: string;
	grouped?: string;
}

const after = (words: string): string => (words ? `: ${words}` : '');

/** ``said`` of an item, once a claim the run took only for this write is given back (kept, and said, where the item
 *  had been sent back). */
async function letGo(item: ItemWrite, said: Said): Promise<Required<Refusal>> {
	const { verb, why, grouped = why } = said;
	if (item.claimed === null)
		return { reason: `${verb}${after(why)}`, group: `${verb}${after(grouped)}` };
	if (item.claimed.from !== 'open')
		return {
			reason: `${verb}${after(why)}; ${KEPT[item.claimed.from]}`,
			group: `${verb}${after(grouped)}; ${KEPT[item.claimed.from]}`,
		};
	const released = await item.doors.release(item.task);
	return released.ok
		? {
				reason: `${verb}, and released again${after(why)}`,
				group: `${verb}, and released again${after(grouped)}`,
			}
		: {
				reason: `${verb}${after(why)}; it stays claimed by you (${released.detail})`,
				group: `${verb}${after(grouped)}; it stays claimed by you`,
			};
}

/** How a refused write is judged: in ``verb``'s words, and, where it may have landed (`mayHaveLanded`), by what
 *  ``landed`` finds the item holds now (null: it did not land). */
interface Judged {
	verb: string;
	landed: () => Promise<Outcome | null>;
}

/** The outcome of a write to ``item`` refused with ``failed``: what it holds now where the write may have landed (this
 *  write, done; or what was asked already so, unchanged, with the claim the run took handled as for a refusal), else
 *  that refusal, with a claim the run cannot use given back. */
async function refusedOr(
	item: ItemWrite,
	failed: { status: number; detail: string },
	judged: Judged,
): Promise<Outcome> {
	if (mayHaveLanded(failed.status)) {
		const now = await judged.landed();
		if (now?.status === 'unchanged') return { ...now, ...(await giveBackClaim(item)) };
		if (now) return now;
	}
	if (item.claimed === null) return { status: 'refused', reason: failed.detail };
	return {
		status: 'refused',
		reason: (await letGo(item, { verb: judged.verb, why: failed.detail })).reason,
	};
}

/** Give back the claim the run took for ``item`` where it was open: what stays the reader's, said. */
async function giveBackClaim(item: ItemWrite): Promise<{ stays?: Refusal }> {
	if (item.claimed === null) return {};
	if (item.claimed.from !== 'open') return { stays: { reason: KEPT[item.claimed.from] } };
	const released = await item.doors.release(item.task);
	return released.ok ? {} : { stays: { reason: `it stays claimed by you (${released.detail})` } };
}

/** A write to ``item`` that wrote ``revision``, with the claim the run took for it. */
const doneBy = (revision: number, item: ItemWrite): Done => ({
	status: 'done',
	revision,
	...(item.claimed ? { claimed: item.claimed } : {}),
});

/** What one save of an item's work changes: the rows it adds, the proposals it keeps (as the canvas keeps one, an edit
 *  of its status) and the rows it removes. */
interface RowChange {
	inserts: InsertRow[];
	keeps: readonly string[];
	deletes: readonly string[];
}

/** The save body of ``change`` built on ``revision``. */
function savePayload(change: RowChange, revision: number): SavePayload {
	return {
		edits: change.keeps.map((id) => ({ id, status: 'accepted' })),
		inserts: change.inserts,
		geometry: [],
		temporal: [],
		spans: [],
		deletes: [...change.deletes],
		base_version: revision,
	};
}

/** The rows a grid label's ``change`` writes. */
const labelRows = (change: Change): RowChange => ({
	inserts: change.inserts.map((t) =>
		makeInsertRow({ id: t.id, shape_type: 'tag', label: t.label }),
	),
	keeps: change.accepts.map((t) => t.id),
	deletes: change.deletes.map((t) => t.id),
});

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

/** What a label run applies: ``intent`` over the grid's labels (``grid``), to every item but those in ``keep`` (items a
 *  person chose to keep as they are), each new tag's id from ``mint``. */
export interface LabelAsk {
	intent: Intent;
	grid: ReadonlySet<string>;
	keep?: ReadonlySet<string>;
	mint?: () => string;
}

/** Apply ``label`` to each of ``targets``; each item's outcome, in order. */
export function applyIntent(
	targets: readonly TaskDetail[],
	label: LabelAsk,
	{ doors, control }: Run,
): Promise<Outcome[]> {
	const { intent, grid, keep = new Set(), mint = () => crypto.randomUUID() } = label;
	return pooled(
		targets,
		async (task): Promise<Outcome> => {
			if (keep.has(task.task_id))
				return { status: 'not-attempted', reason: 'kept as it is, with what its label carries' };
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
			const took = await taken(task, may.kind, doors);
			if (!took.ok) return took.outcome;
			const item = took.value;
			const saved = await doors.save(task, savePayload(labelRows(change), work.revision));
			if (saved.ok) return doneBy(saved.data.version, item);
			// Reported by what the item holds now: labelled as asked (this write landed, or another one did), or refused.
			return refusedOr(item, saved, {
				verb: 'not labelled',
				landed: async () => {
					const fresh = await doors.reread(task);
					if (!fresh?.work) return null;
					// This save landed where the item holds the tags it minted: done, its undo naming the revision only
					// while the work is still at the one it wrote. Otherwise what was asked was already so.
					const held = new Set(fresh.work.tags.map((t) => t.id));
					const minted = change.inserts.map((t) => t.id);
					if (minted.length > 0 && minted.every((id) => held.has(id))) {
						const at = fresh.work.revision;
						return { ...doneBy(at, item), revision: at === work.revision + 1 ? at : null };
					}
					return isEmpty(changeFor(fresh.work.tags, intent, grid, mint))
						? { status: 'unchanged' }
						: null;
				},
			});
		},
		control,
		NOT_STARTED,
	);
}

/** Keep or remove every proposal of each of ``targets``; each item's outcome, in order. An item holding none is
 *  already so. */
export function settleEach(
	targets: readonly TaskDetail[],
	action: 'keep' | 'remove',
	{ doors, control }: Run,
): Promise<Outcome[]> {
	return pooled(
		targets,
		async (task): Promise<Outcome> => {
			const may = labelable(task);
			if (may.kind === 'no') return { status: 'refused', reason: may.reason };
			const work = task.work;
			if (!work || work.proposals === 0) return { status: 'unchanged' };
			const took = await taken(task, may.kind, doors);
			if (!took.ok) return took.outcome;
			const item = took.value;
			const settled = await doors.settle(task, action, work.revision);
			if (settled.ok)
				return settled.data.settled === 0
					? { status: 'unchanged' }
					: doneBy(settled.data.version, item);
			// Reported by what the item holds now: no proposal left is what was asked (this call landed, or another did).
			return refusedOr(item, settled, {
				verb: 'not changed',
				landed: async () => {
					const fresh = await doors.reread(task);
					return fresh?.work && fresh.work.proposals === 0 ? { status: 'unchanged' } : null;
				},
			});
		},
		control,
		NOT_STARTED,
	);
}

/** How a summary groups an ask the model did not answer: by what failed (the problem's `type`), never by the words the
 *  endpoint answered for one item, which its cell keeps. */
const UNANSWERED = {
	'model-endpoint-timeout': 'the model did not answer in time',
	'model-endpoint-busy': 'the model endpoint was busy',
	'model-endpoint-failed': 'the model endpoint failed',
	'no-model-endpoint': 'the model endpoint is unavailable',
	'model-refused-ask': 'the model refused the ask',
} as const satisfies Record<string, string>;

const isUnansweredType = (type: string | undefined): type is keyof typeof UNANSWERED =>
	type !== undefined && Object.hasOwn(UNANSWERED, type);

/** What a summary groups an ask the model did not answer by. */
function unanswered(failed: { status: number; detail: string; type?: string }): string {
	if (isUnansweredType(failed.type)) return UNANSWERED[failed.type];
	return failed.status === 0 ? 'no answer came' : failed.detail;
}

/** How a summary groups a proposal the item's classes cannot hold (the annotator's `dropped`). */
const NO_FIT = "a shape this item's classes do not take";

/** What an ask run asks: ``ask`` of ``model``, which is asked only about the items it fits. */
export interface ModelAsk {
	model: Pick<AssistModel, 'returns'>;
	ask: AssistAsk;
}

/** Whether ``task`` may be asked ``model`` about: how it is taken for the write, or why it is not asked at all. */
function eligibility(
	task: TaskDetail,
	model: Pick<AssistModel, 'returns'>,
): Step<'edit' | 'claim-first'> {
	const may = labelable(task);
	if (may.kind === 'no') return { ok: false, outcome: { status: 'refused', reason: may.reason } };
	const kind = task.media?.kind ?? 'image';
	if (kind !== 'image')
		return {
			ok: false,
			outcome: {
				status: 'refused',
				reason: `a model is shown a picture, and this item is ${kind}`,
			},
		};
	if (!fits(model, task))
		return {
			ok: false,
			outcome: {
				status: 'refused',
				reason: `it returns ${model.returns.join(', ') || 'nothing named'}, no shape this item's classes take`,
				group: NO_FIT,
			},
		};
	return { ok: true, value: may.kind };
}

/** Ask ``asked.model`` about each of ``targets`` and save its answer to the item as proposals; each item's outcome, in
 *  order. An item whose classes take no shape the model returns is not asked. */
export function askEach(
	targets: readonly TaskDetail[],
	asked: ModelAsk,
	{ doors, control }: Run,
): Promise<Outcome[]> {
	return pooled(
		targets,
		async (task): Promise<Outcome> => {
			if (control.signal.aborted) return NOT_STARTED;
			const may = eligibility(task, asked.model);
			if (!may.ok) return may.outcome;
			const took = await taken(task, may.value, doors);
			if (!took.ok) return took.outcome;
			return askItem(took.value, asked.ask, control.signal);
		},
		control,
		NOT_STARTED,
	);
}

/** Ask the model about ``item`` and save what it answered, or say why not. */
async function askItem(item: ItemWrite, ask: AssistAsk, signal: AbortSignal): Promise<Outcome> {
	const answered = await item.doors.assist(item.task, ask, signal);
	if (signal.aborted) return stoppedWhileAsked(item);
	if (!answered.ok)
		return {
			status: 'refused',
			...(await letGo(item, {
				verb: 'not answered',
				why: answered.detail,
				grouped: unanswered(answered),
			})),
		};
	const held = await heldProposals(item, answered.data.model, signal);
	if (!held.ok) return held.outcome;
	if (answered.data.proposals.length === 0) return nothingProposed(item, answered.data, held.value);
	return saveAnswer(item, answered.data, held.value);
}

/** The outcome of an ask given up (the person stopped the run, or left the page), its claim given back. */
async function stoppedWhileAsked(item: ItemWrite): Promise<Outcome> {
	return {
		status: 'not-attempted',
		reason: (await letGo(item, { verb: 'stopped while it was asked', why: '' })).reason,
	};
}

/** An answer with no proposal: what it proposed fitted none of the item's classes (a refusal), or it found nothing,
 *  which leaves the proposals of an earlier ask of the model (``held``) as they are, said on the item. */
async function nothingProposed(
	item: ItemWrite,
	answer: AssistAnswer,
	held: readonly string[],
): Promise<Outcome> {
	if (answer.dropped.length > 0)
		return {
			status: 'refused',
			...(await letGo(item, {
				verb: 'nothing it proposed fits',
				why: answer.dropped.join('; '),
				grouped: NO_FIT,
			})),
		};
	const found = held.length > 0 ? { note: 'found nothing: its earlier proposals stay' } : {};
	if (item.claimed === null) return { status: 'unchanged', ...found };
	if (item.claimed.from !== 'open')
		return { status: 'unchanged', ...found, stays: { reason: KEPT[item.claimed.from] } };
	const released = await item.doors.release(item.task);
	return released.ok
		? { status: 'unchanged', ...found }
		: {
				status: 'refused',
				reason: `it found nothing; it stays claimed by you (${released.detail})`,
			};
}

/** Save ``answer`` to ``item`` as proposals on the revision the grid read, in place of the proposals of the same model it
 *  still holds (``replaced``). */
async function saveAnswer(
	item: ItemWrite,
	answer: AssistAnswer,
	replaced: readonly string[],
): Promise<Outcome> {
	const base = item.task.work?.revision ?? 0;
	const inserts = answer.proposals.map((p) => makeInsertRow(proposalFields(answer, p)));
	const saved = await item.doors.save(
		item.task,
		savePayload({ inserts, keeps: [], deletes: replaced }, base),
	);
	const done = answerSaved(item, answer);
	if (saved.ok) return done(saved.data.version);
	// Reported by what the item holds now: the rows this save wrote (it landed), or refused.
	return refusedOr(item, saved, {
		verb: 'not saved',
		landed: () => recoverLostSave(item, { ids: inserts.map((r) => r.id), base }, done),
	});
}

/** The proposals of ``model`` that ``item`` still holds, which its new answer replaces: read from the item's rows only
 *  where the listing counts a proposal on it, and given up with the run, so no save starts once it is stopped. */
async function heldProposals(
	item: ItemWrite,
	model: string,
	signal: AbortSignal,
): Promise<Step<string[]>> {
	if ((item.task.work?.proposals ?? 0) === 0) return { ok: true, value: [] };
	const read = await item.doors.proposals(item.task, modelSource(model), signal);
	if (signal.aborted) return { ok: false, outcome: await stoppedWhileAsked(item) };
	if (read.ok) return { ok: true, value: read.data.ids };
	const why = `its proposals could not be read (${read.detail})`;
	return {
		ok: false,
		outcome: { status: 'refused', ...(await letGo(item, { verb: 'not saved', why })) },
	};
}

/** The outcome of ``answer`` saved to ``item`` at a revision (null: it has no undo of its own). What of the answer the
 *  item's classes could not hold is named on it beside what it kept. */
const answerSaved = (item: ItemWrite, answer: AssistAnswer) => {
	// The tags it proposed are said from the item's listing; what else it proposed only the answer says.
	const shapes = shapesProposed(answer);
	return (revision: number | null): Done => ({
		status: 'done',
		revision,
		...(item.claimed ? { claimed: item.claimed } : {}),
		...(shapes ? { proposed: shapes } : {}),
		...(answer.dropped.length > 0
			? { left: { reason: answer.dropped.join('; '), group: NO_FIT } }
			: {}),
	});
};

/** What a save of the rows ``saved.ids`` built on ``saved.base`` whose answer was lost did: ``done`` where the item holds
 *  them (it landed), else null. */
async function recoverLostSave(
	item: ItemWrite,
	saved: { ids: readonly string[]; base: number },
	done: (revision: number | null) => Done,
): Promise<Done | null> {
	const at = await item.doors.revisionHolding(item.task, saved.ids);
	// The save from `base` wrote `base + 1`: at a later revision another save landed on top, and an undo naming that one
	// would take it back, so the item is reported saved with no undo of its own.
	return at === null ? null : done(at === saved.base + 1 ? at : null);
}

/** Undo or redo one save on the server, by the revision ``written`` wrote: accepted while the work is as that save left
 *  it, at its revision or with every save since taken back (so an item's saves are undone one after another, and redone
 *  the same way, each naming its own revision); a 409 for work that differs, which is left as it is. */
async function revert({ task, revision }: Written, doors: Doors): Promise<Done | Refused> {
	const reverted = await doors.revert(task, revision);
	if (reverted.ok) return { status: 'done', revision: reverted.data.version };
	// An undo whose answer was lost, or resent after it landed: the work holds what it held before that save.
	const now = mayHaveLanded(reverted.status) ? await doors.undone(task, revision) : null;
	return now === null
		? { status: 'refused', reason: reverted.detail }
		: { status: 'done', revision: now };
}

/** A refused undo or redo in ``verb``'s words: the step leaves the grid's stack, so what was not done is named. */
const said = (refused: Refused, verb: string): Refused => ({
	status: 'refused',
	reason: `${verb}: ${refused.reason}`,
});

/** Undo each of ``written`` on the server, then give back a claim its run took on an item that was open, where that
 *  claim is still the one the item is held under: read once, its holder and the instant its last claim was taken are
 *  the ones the run's claim answered (`claimMark`). A claim the person let go and took again since (another tab, the
 *  canvas, a lapsed lease claimed again, a manager's release) is theirs, and is left with nothing said; one taken on an
 *  item that had been sent back stays, and so does one the read or the release failed for, each said on the item. */
export function undoEach(written: readonly Written[], { doors, control }: Run): Promise<Outcome[]> {
	return pooled(
		written,
		async (entry): Promise<Outcome> => {
			const reverted = await revert(entry, doors);
			if (reverted.status !== 'done') return said(reverted, 'not undone');
			if (entry.claimed === null) return reverted;
			if (entry.claimed.from !== 'open')
				return { ...reverted, claimed: entry.claimed, stays: { reason: KEPT[entry.claimed.from] } };
			return { ...reverted, ...(await giveBack(entry.task, entry.claimed, doors)) };
		},
		control,
		NOT_STARTED,
	);
}

/** Release ``task``'s claim where it is still the one ``claim`` names: what the undo's outcome says of it. */
async function giveBack(
	task: TaskDetail,
	claim: ClaimTaken,
	doors: Doors,
): Promise<Pick<Done, 'claimed' | 'stays'>> {
	if (claim.mark === null) return {};
	const now = await doors.reread(task);
	if (now === null) return { stays: { reason: CLAIM_UNREAD } };
	if (claimMark(now) !== claim.mark) return {};
	const released = await doors.release(task);
	if (released.ok) return { claimed: claim };
	return { stays: { reason: `it stays claimed by you (${released.detail})` } };
}

/** Redo each of ``written`` (what an undo wrote) on the server, the claim its undo gave back taken again first, as the
 *  run that wrote it took it; a claim the redo cannot use goes back. */
export function redoEach(written: readonly Written[], { doors, control }: Run): Promise<Outcome[]> {
	return pooled(
		written,
		async (entry): Promise<Outcome> => {
			if (entry.claimed?.from !== 'open') {
				const reverted = await revert(entry, doors);
				if (reverted.status !== 'done') return said(reverted, 'not redone');
				return entry.claimed ? { ...reverted, claimed: entry.claimed } : reverted;
			}
			const took = await taken(entry.task, 'claim-first', doors);
			if (!took.ok) return took.outcome;
			const reverted = await revert(entry, doors);
			if (reverted.status === 'done' && took.value.claimed)
				return { ...reverted, claimed: took.value.claimed };
			if (reverted.status === 'done') return reverted;
			return {
				status: 'refused',
				reason: (await letGo(took.value, { verb: 'not redone', why: reverted.reason })).reason,
			};
		},
		control,
		NOT_STARTED,
	);
}
