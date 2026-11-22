/**
 * Which of a project's items a screen shows, as the page's address holds it: the data manager's filters, sort and
 * page, the browse and review passes' filters. THE ADDRESS IS THE VIEW: a link, a reload and Back restore the screen
 * with its filters, so every value here is read from the URL (`readItemQuery`), validated and bounded, and written back
 * into it (`writeItemQuery`) leaving out what is the default. The annotator filters, sorts and pages (`doorParams`);
 * the browser never holds more than one page.
 */
import type { TaskState } from './types.ts';

export const TASK_STATES: readonly TaskState[] = [
	'unassigned',
	'claimed',
	'changes_requested',
	'in_review',
	'accepted',
	'skipped',
];
export const SORTS = [
	'item',
	'state',
	'assignee',
	'sent',
	'updated',
	'submitted',
	'reviewed',
	'sampling',
] as const;
export type Sort = (typeof SORTS)[number];
export const DATE_FIELDS = ['updated', 'sent', 'submitted', 'reviewed'] as const;
export type DateField = (typeof DATE_FIELDS)[number];
const PAGE_SIZES = [10, 25, 50, 100] as const;
/** A screen's walk through a project's items on the canvas: labeling your claims, browsing labeled work, or reviewing. */
export const PASSES = ['label', 'browse', 'review'] as const;
export type Pass = (typeof PASSES)[number];

/** The longest value a text filter takes, as the annotator bounds it. */
const MAX_FILTER = 512;
/** An address's query as a page reads it (`page.url.searchParams` is read-only). */
type Params = Pick<URLSearchParams, 'get' | 'getAll' | 'toString'>;
/** The deepest page the address may name: the annotator reads at most 100,000 items deep. */
const MAX_OFFSET = 100_000;
const DAY = /^\d{4}-\d{2}-\d{2}$/;

export interface ItemQuery {
	states: TaskState[];
	assignee: string | null;
	reviewer: string | null;
	submitter: string | null;
	label: string | null;
	dataset: string | null;
	/** Only the reader's own work (`mine`), or everyone's (`all`, for someone who may review). */
	whose: 'mine' | 'all';
	date: DateField;
	/** Days, `YYYY-MM-DD`, read in the reader's own time zone: `since` from its first moment, `until` through its last. */
	since: string | null;
	until: string | null;
	sort: Sort;
	order: 'asc' | 'desc';
	/** 1-based. */
	page: number;
	size: number;
}

export const DEFAULT_QUERY: ItemQuery = {
	states: [],
	assignee: null,
	reviewer: null,
	submitter: null,
	label: null,
	dataset: null,
	whose: 'all',
	date: 'updated',
	since: null,
	until: null,
	sort: 'item',
	order: 'asc',
	page: 1,
	size: 25,
};

/** The names the address holds the query under. Each tab writes only these, so a filter never leaks across tabs. */
const KEYS = [
	'state',
	'assignee',
	'reviewer',
	'submitter',
	'label',
	'dataset',
	'whose',
	'date',
	'since',
	'until',
	'sort',
	'order',
	'page',
	'size',
] as const;

function text(value: string | null): string | null {
	const trimmed = value?.trim() ?? '';
	return trimmed === '' || trimmed.length > MAX_FILTER ? null : trimmed;
}

const pick = <T extends string>(allowed: readonly T[], value: string | null, fallback: T): T =>
	allowed.includes(value as T) ? (value as T) : fallback;

/** The query an address names; a value it cannot honour is left at its default rather than guessed. ``sort`` is the
 *  order an address that names none is in: the project's sampling order on the Items tab while its strategy is on. */
export function readItemQuery(params: Params, sort: Sort = DEFAULT_QUERY.sort): ItemQuery {
	const size = Number(params.get('size'));
	const sizeOk = (PAGE_SIZES as readonly number[]).includes(size);
	const page = Number(params.get('page'));
	const day = (name: string): string | null => {
		const value = params.get(name);
		return value !== null && DAY.test(value) && !Number.isNaN(Date.parse(value)) ? value : null;
	};
	const effectiveSize = sizeOk ? size : DEFAULT_QUERY.size;
	return {
		states: [...new Set(params.getAll('state'))].filter((s): s is TaskState =>
			(TASK_STATES as readonly string[]).includes(s),
		),
		assignee: text(params.get('assignee')),
		reviewer: text(params.get('reviewer')),
		submitter: text(params.get('submitter')),
		label: text(params.get('label')),
		dataset: text(params.get('dataset')),
		whose: params.get('whose') === 'mine' ? 'mine' : 'all',
		date: pick(DATE_FIELDS, params.get('date'), DEFAULT_QUERY.date),
		since: day('since'),
		until: day('until'),
		sort: pick(SORTS, params.get('sort'), sort),
		order: params.get('order') === 'desc' ? 'desc' : 'asc',
		page:
			Number.isInteger(page) && page >= 1 && (page - 1) * effectiveSize <= MAX_OFFSET ? page : 1,
		size: effectiveSize,
	};
}

/** The address with ``query`` written into it over ``base``'s other names (the tab, the project), defaults left out;
 *  ``sort`` is the order an address that names none is in, as `readItemQuery` takes it. */
export function writeItemQuery(
	query: ItemQuery,
	base: Params,
	sort: Sort = DEFAULT_QUERY.sort,
): URLSearchParams {
	const out = new URLSearchParams(base.toString());
	for (const key of KEYS) out.delete(key);
	for (const state of query.states) out.append('state', state);
	const fields: [string, string | null, string | null][] = [
		['assignee', query.assignee, null],
		['reviewer', query.reviewer, null],
		['submitter', query.submitter, null],
		['label', query.label, null],
		['dataset', query.dataset, null],
		['whose', query.whose, DEFAULT_QUERY.whose],
		['date', query.date, DEFAULT_QUERY.date],
		['since', query.since, null],
		['until', query.until, null],
		['sort', query.sort, sort],
		['order', query.order, DEFAULT_QUERY.order],
		['page', String(query.page), String(DEFAULT_QUERY.page)],
		['size', String(query.size), String(DEFAULT_QUERY.size)],
	];
	for (const [key, value, fallback] of fields)
		if (value !== null && value !== fallback) out.set(key, value);
	return out;
}

/** Whether any filter narrows the query (the sort and the page do not). */
export function filtering(query: ItemQuery): boolean {
	return (
		query.states.length > 0 ||
		[
			query.assignee,
			query.reviewer,
			query.submitter,
			query.label,
			query.dataset,
			query.since,
			query.until,
		].some((v) => v !== null) ||
		query.whose === 'mine'
	);
}

/** A day's first moment in the reader's own time zone, as an instant with its offset (what the annotator takes). */
function dayStart(day: string, plusDays = 0): string {
	const [y, m, d] = day.split('-').map(Number);
	const local = new Date(y ?? 1970, (m ?? 1) - 1, (d ?? 1) + plusDays);
	return local.toISOString();
}

/** The annotator's listing parameters for ``query`` (`GET /projects/{id}/tasks`): ``me`` is who `whose=mine` means. */
export function doorParams(query: ItemQuery, me: string | null): URLSearchParams {
	const out = new URLSearchParams();
	for (const state of query.states) out.append('state', state);
	const fields: [string, string | null][] = [
		['assignee', query.assignee],
		['reviewer', query.reviewer],
		['submitter', query.submitter],
		['label', query.label],
		['dataset', query.dataset],
		['holder', query.whose === 'mine' ? me : null],
	];
	for (const [key, value] of fields) if (value !== null) out.set(key, value);
	if (query.since !== null || query.until !== null) out.set('date', query.date);
	if (query.since !== null) out.set('since', dayStart(query.since));
	if (query.until !== null) out.set('until', dayStart(query.until, 1));
	out.set('sort', query.sort);
	out.set('order', query.order);
	return out;
}

/** The pass a canvas address names (`?pass=`), labeling your claims when it names none. */
export function readPass(params: Params): Pass {
	return pick(PASSES, params.get('pass'), 'label');
}

/** What a pass walks, as a query: your live claims; the labeled work a browse filter names (items whose work holds a
 *  shape or a link, `has_work`: a claim nobody has drawn on is not labeled work); the work in review that someone other
 *  than you submitted, in the judge's order (the items it flagged first, then those it has not read, then those it found
 *  nothing in, each the longest waiting first), so the Review tab and the canvas walking it list one order. The canvas
 *  reads it one page around the open item. */
export function passParams(pass: Pass, query: ItemQuery, me: string | null): URLSearchParams {
	if (pass === 'label') {
		const out = new URLSearchParams({ state: 'claimed', sort: 'item' });
		if (me !== null) out.set('assignee', me);
		return out;
	}
	if (pass === 'review') {
		const out = doorParams({ ...query, states: ['in_review'] }, me);
		if (me !== null) out.set('not_submitter', me);
		out.set('sort', 'judge');
		out.set('order', 'asc');
		return out;
	}
	const out = doorParams(
		{ ...query, states: query.states.length > 0 ? query.states : [...LABELED_STATES] },
		me,
	);
	out.set('has_work', 'true');
	return out;
}

/** Where a reviewer is in the review pass they started, carried in the address beside its filter: `of`, how many items
 *  waited for them when the pass started (decided then, never read again), and `done`, how many they have judged since.
 *  Each item judged leaves the walk, so the walk alone would read "1 of N" on every item while N shrinks. */
export interface PassCount {
	of: number | null;
	done: number;
}

/** The most items a pass counts: a project's deepest offset, so no address names more. */
const MAX_PASS = MAX_OFFSET + 200;

/** The pass count an address names; a value it cannot honour is none (`of`) or nothing judged (`done`). */
export function readPassCount(params: Params): PassCount {
	const count = (name: string): number | null => {
		const value = Number(params.get(name) ?? Number.NaN);
		return Number.isInteger(value) && value >= 0 && value <= MAX_PASS ? value : null;
	};
	const of = count('of');
	const done = count('done') ?? 0;
	return { of: of !== null && of > 0 ? of : null, done: of !== null ? Math.min(done, of) : done };
}

/** ``filter`` with the pass count written beside it. */
export function withPassCount(filter: URLSearchParams, count: PassCount): URLSearchParams {
	const out = new URLSearchParams(filter);
	if (count.of !== null) out.set('of', String(count.of));
	if (count.done > 0) out.set('done', String(count.done));
	else out.delete('done');
	return out;
}

/** The window a review pass's page of still-waiting items (at `offset` in the walk, `waiting` in all) sits in: an
 *  item's place is the verdicts given in the pass plus its place among those still waiting, out of what waited when the
 *  pass started. Robust to another reviewer judging an item meanwhile: that item leaves the walk, so it is neither
 *  counted as done here nor met again, and the pass ends (back at the Review tab) short of its last number; new work
 *  sent into review meanwhile grows the total rather than pushing a place past it. */
export function passWindow(
	count: PassCount,
	offset: number,
	waiting: number,
): { offset: number; total: number } {
	return { offset: count.done + offset, total: Math.max(count.of ?? 0, count.done + waiting) };
}

/** The states a browse looks through by default: everything someone has labeled, finished or not. */
export const LABELED_STATES: readonly TaskState[] = [
	'claimed',
	'in_review',
	'changes_requested',
	'accepted',
];
