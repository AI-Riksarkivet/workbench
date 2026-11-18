/**
 * The bulk-labeling grid's rules, pure: which classes a grid labels, what one label does to one item's tags, where a key
 * moves the focus, and the grid's view in the address. The grid (`LabelGrid.svelte`) reads and writes through the
 * annotator's own doors; what a reader may do on an item is that item's `legal_events`, never decided here.
 *
 * WHICH CLASSES. A grid label is one decision about the whole item: a class the ontology lets be drawn as a `tag` (the
 * canvas's item-level choices, `controller.tagClasses`). A class drawn only as a region (a box, a polygon, a mask, a
 * keypoint, a polyline, a stretch of time) or a text span marks a PART of the item, so it is labelled on the canvas; a
 * class with no tools named may be drawn with anything, and is a region class to the canvas, so it is too. A tag class
 * whose attribute is required cannot be satisfied by a tag that answers none, so it is labelled on the canvas as well.
 */
import type { ClassDef } from '#lib/canvas/classes.ts';
import type { TaskDetail } from './types.ts';

/** One of the project's classes as the grid offers it: a grid label, or one to label on the canvas and why. */
export interface GridClass extends ClassDef {
	/** The digit that applies it (1-9 for the first nine grid labels), or null. */
	key: number | null;
}

/** The project's classes, split: those a grid label applies, in the ontology's order with their digits, and the rest
 *  with what they are drawn with (the canvas labels them). */
export function gridClasses(
	classes: readonly {
		name: string;
		colour?: string | null;
		tools?: readonly string[];
		required?: boolean;
		attributes?: readonly { required?: boolean }[];
	}[],
	colours: ReadonlyMap<string, string>,
): { labels: GridClass[]; canvas: { name: string; why: string }[] } {
	const labels: GridClass[] = [];
	const canvas: { name: string; why: string }[] = [];
	for (const c of classes) {
		const tools = c.tools ?? [];
		if (!tools.includes('tag')) {
			canvas.push({ name: c.name, why: tools.length > 0 ? tools.join(', ') : 'any shape' });
			continue;
		}
		if ((c.attributes ?? []).some((a) => a.required)) {
			canvas.push({ name: c.name, why: 'a required attribute' });
			continue;
		}
		labels.push({
			name: c.name,
			colour: colours.get(c.name) ?? null,
			tools,
			required: c.required ?? false,
			key: labels.length < 9 ? labels.length + 1 : null,
		});
	}
	return { labels, canvas };
}

/** One whole-item tag of an item's work, as the listing answers it: whether it carries more than its class (text,
 *  attributes, a link: what a new label would discard) and whether it is a proposal no person has kept yet. */
export interface Tag {
	id: string;
	label: string | null;
	detailed: boolean;
	prediction: boolean;
}

/** What a person asks of the selection: set one label (the item's other grid labels go), add or take away one label
 *  beside the others, or clear every grid label. */
export type Intent =
	| { kind: 'set'; label: string }
	| { kind: 'toggle'; label: string }
	| { kind: 'clear' };

/** One item's change: the labels it adds, the tags it removes, and the proposals of the label asked for it keeps (a
 *  person choosing the class a model proposed confirms the proposal, as accepting it on the canvas does). */
export interface Change {
	inserts: { id: string; label: string }[];
	deletes: Tag[];
	accepts: Tag[];
}

/** What ``intent`` does to an item holding ``tags``: only tags of a grid label (``grid``) are ever removed, so a tag of
 *  any other class, and every region, is left as it is. An intent the item already satisfies changes nothing. */
export function changeFor(
	tags: readonly Tag[],
	intent: Intent,
	grid: ReadonlySet<string>,
	mint: () => string,
): Change {
	const ours = tags.filter((t) => t.label !== null && grid.has(t.label));
	const none: Change = { inserts: [], deletes: [], accepts: [] };
	if (intent.kind === 'clear') return { ...none, deletes: ours };
	const same = ours.filter((t) => t.label === intent.label);
	const kept = same.filter((t) => !t.prediction);
	const proposed = same.filter((t) => t.prediction);
	// The label asked for is the person's once a tag of it is theirs, or a proposal of it is confirmed.
	const take: Change =
		kept.length > 0 || proposed.length > 0
			? { ...none, accepts: proposed }
			: { ...none, inserts: [{ id: mint(), label: intent.label }] };
	if (intent.kind === 'toggle')
		// Off when the person already holds it; a label held only as a proposal is confirmed, not taken away.
		return kept.length > 0 ? { ...none, deletes: kept } : take;
	return { ...take, deletes: ours.filter((t) => t.label !== intent.label) };
}

export const isEmpty = (change: Change): boolean =>
	change.inserts.length === 0 && change.deletes.length === 0 && change.accepts.length === 0;

/** Whether ``change`` discards more than a class: a tag it removes carries text, attributes or a link, or is a model's
 *  proposal. Such an item is asked about before a label replaces it. */
export const discards = (change: Change): boolean =>
	change.deletes.some((t) => t.detailed || t.prediction);

/** Whether an item's grid labels carry more than their class (shown on its cell). */
export const detailedIn = (tags: readonly Tag[], grid: ReadonlySet<string>): boolean =>
	tags.some((t) => t.label !== null && grid.has(t.label) && (t.detailed || t.prediction));

/** What a person may do to one item's label now, read from what the annotator answered for it: its `legal_events`
 *  (what THEY may fire on it, `machines.caller_events`) and its work. */
export type Labelable = { kind: 'edit' } | { kind: 'claim-first' } | { kind: 'no'; reason: string };

/** The bound on an item's tags the listing answers past which a label is the canvas's. */
const TAGS_CUT = 'it holds more tags than a grid reads: open it on the canvas';

export function labelable(task: TaskDetail): Labelable {
	if (task.work === null) return { kind: 'no', reason: 'you may not read this item’s work' };
	if (task.work === undefined) return { kind: 'no', reason: 'its work was not read' };
	if (task.work.tags_cut) return { kind: 'no', reason: TAGS_CUT };
	if (!task.source.where || !task.source.keys[0])
		return { kind: 'no', reason: 'it names no unit of a dataset' };
	const events = new Set(task.legal_events.map((e) => e.event));
	// A holder's own claim takes edits (`save_draft`), and so does work in review for who may fix it (`fix_and_accept`).
	if (events.has('save_draft') || events.has('fix_and_accept')) return { kind: 'edit' };
	if (events.has('claim')) return { kind: 'claim-first' };
	if (task.state === 'claimed') return { kind: 'no', reason: 'another person holds it' };
	return {
		kind: 'no',
		reason: `it is ${task.state.replace('_', ' ')}: nothing about it is yours to change now`,
	};
}

/** Whether ``label`` is one of the item's own classes: those it was sent with (its captured ontology), which its submit
 *  is judged by. An item sent with no classes takes any. */
export function itemTakes(task: TaskDetail, label: string): boolean {
	const classes = task.ontology?.classes ?? [];
	return classes.length === 0 || classes.some((c) => c.name === label && c.tools.includes('tag'));
}

/** The grid labels an item holds as a person's (``proposed`` false) or only as a model's proposal no person has kept
 *  (``proposed`` true), in the grid's order. */
export function gridLabelsOf(
	tags: readonly Tag[],
	grid: readonly GridClass[],
	proposed = false,
): GridClass[] {
	const kept = new Set(tags.filter((t) => !t.prediction).map((t) => t.label));
	const held = new Set(tags.filter((t) => t.prediction && !kept.has(t.label)).map((t) => t.label));
	return grid.filter((c) => (proposed ? held : kept).has(c.name));
}

// ── focus and selection ──────────────────────────────────────────────────────────────────────

/** The grid's shape: how many cells, how many to a row, and how many rows a page key moves. */
export interface Shape {
	count: number;
	columns: number;
	pageRows: number;
}

/** Where ``key`` moves the focus from ``at``, or null when it is not a moving key. Arrows move by one cell or one row,
 *  Home and End to the row's ends (with Ctrl or Cmd to the grid's), Page Up and Page Down by ``pageRows`` rows; the
 *  focus stays inside the grid. */
export function moved(at: number, key: string, ctrl: boolean, shape: Shape): number | null {
	const { count, columns, pageRows } = shape;
	if (count === 0) return null;
	const cols = Math.max(1, columns);
	const rowStart = at - (at % cols);
	const target = (() => {
		switch (key) {
			case 'ArrowLeft':
				return at - 1;
			case 'ArrowRight':
				return at + 1;
			case 'ArrowUp':
				return at - cols;
			case 'ArrowDown':
				return at + cols;
			case 'Home':
				return ctrl ? 0 : rowStart;
			case 'End':
				return ctrl ? count - 1 : rowStart + cols - 1;
			case 'PageUp':
				return at - cols * Math.max(1, pageRows);
			case 'PageDown':
				return at + cols * Math.max(1, pageRows);
			default:
				return null;
		}
	})();
	if (target === null) return null;
	// An arrow up from the first row or down from the last stays in its column's end rather than leaving the grid.
	if (target < 0) return key === 'ArrowLeft' ? 0 : at % cols;
	if (target >= count) {
		if (key === 'ArrowDown' || key === 'PageDown') {
			const last = count - 1;
			return last - (last % cols) + (at % cols) <= last ? last - (last % cols) + (at % cols) : last;
		}
		return count - 1;
	}
	return target;
}

/** The cells between two, both included, in order. */
export function between(a: number, b: number): number[] {
	const [lo, hi] = a <= b ? [a, b] : [b, a];
	return Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
}

/** The digit of a key event as a grid label's number (`Digit1`…`Digit9` and the keypad's), or null: read from the
 *  physical key, so it holds with Alt held and on any keyboard layout. */
export function digitOf(code: string): number | null {
	const match = /^(?:Digit|Numpad)([1-9])$/.exec(code);
	return match ? Number(match[1]) : null;
}

// ── what a cell shows ────────────────────────────────────────────────────────────────────────

/** The longest excerpt of an item's text a cell shows. */
const EXCERPT = 160;

/** ``text`` cut to a cell's excerpt at a word where one is near, with an ellipsis when it was cut. */
export function excerpt(text: string, max = EXCERPT): string {
	const flat = text.replace(/\s+/g, ' ').trim();
	if (flat.length <= max) return flat;
	const cut = flat.slice(0, max);
	const space = cut.lastIndexOf(' ');
	return `${(space > max * 0.6 ? cut.slice(0, space) : cut).trimEnd()}…`;
}

// ── the view in the address ──────────────────────────────────────────────────────────────────

/** Whose items the grid lists: those open to the reader's labeling, or the work waiting for their review. */
export const PASSES = ['label', 'review'] as const;
export type GridPass = (typeof PASSES)[number];
export const GRID_SIZES = [25, 50, 100, 200] as const;

export interface GridQuery {
	pass: GridPass;
	dataset: string | null;
	/** A class on the item (the annotator's `label` filter). */
	label: string | null;
	page: number;
	size: number;
	/** The item the focus is on, kept so a reload comes back to it. */
	at: string | null;
}

export const DEFAULT_GRID: GridQuery = {
	pass: 'label',
	dataset: null,
	label: null,
	page: 1,
	size: 50,
	at: null,
};

const GRID_KEYS = ['pass', 'dataset', 'label', 'page', 'size', 'at'] as const;
/** The longest value the address's text names take, as the annotator bounds its filters. */
const MAX_TEXT = 512;
/** The deepest page the address may name: the annotator reads at most 100,000 items deep. */
const MAX_OFFSET = 100_000;
type Params = Pick<URLSearchParams, 'get' | 'toString'>;

const text = (value: string | null): string | null => {
	const trimmed = value?.trim() ?? '';
	return trimmed === '' || trimmed.length > MAX_TEXT ? null : trimmed;
};

/** The grid's view an address names; a value it cannot honour is left at its default. */
export function readGridQuery(params: Params): GridQuery {
	const size = Number(params.get('size'));
	const sized = (GRID_SIZES as readonly number[]).includes(size) ? size : DEFAULT_GRID.size;
	const page = Number(params.get('page'));
	return {
		pass: params.get('pass') === 'review' ? 'review' : 'label',
		dataset: text(params.get('dataset')),
		label: text(params.get('label')),
		page: Number.isInteger(page) && page >= 1 && (page - 1) * sized <= MAX_OFFSET ? page : 1,
		size: sized,
		at: text(params.get('at')),
	};
}

/** The address with ``query`` written over ``base``'s other names, the defaults left out. */
export function writeGridQuery(query: GridQuery, base: Params): URLSearchParams {
	const out = new URLSearchParams(base.toString());
	for (const key of GRID_KEYS) out.delete(key);
	const fields: [string, string | null, string | null][] = [
		['pass', query.pass, DEFAULT_GRID.pass],
		['dataset', query.dataset, null],
		['label', query.label, null],
		['page', String(query.page), '1'],
		['size', String(query.size), String(DEFAULT_GRID.size)],
		['at', query.at, null],
	];
	for (const [key, value, fallback] of fields)
		if (value !== null && value !== fallback) out.set(key, value);
	return out;
}

/** The annotator's listing parameters for the grid: the items open to ``me``'s labeling (their claims and the items
 *  nobody holds, `open_to`), or the work in review someone other than ``me`` submitted. */
export function gridDoorParams(query: GridQuery, me: string | null): URLSearchParams {
	const out = new URLSearchParams();
	if (query.pass === 'review') {
		out.append('state', 'in_review');
		if (me !== null) out.set('not_submitter', me);
	} else if (me !== null) out.set('open_to', me);
	else
		for (const state of ['unassigned', 'claimed', 'changes_requested']) out.append('state', state);
	if (query.dataset !== null) out.set('dataset', query.dataset);
	if (query.label !== null) out.set('label', query.label);
	out.set('sort', 'item');
	out.set('order', 'asc');
	return out;
}
