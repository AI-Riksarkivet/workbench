/**
 * A view held against the dataset it is applied to (ADR 0022): a view names a table and, through its set-ups, that
 * table's columns, and either may be gone by the time it is opened (a view kept in this browser for a while, or one
 * shared by its link). What is still held is applied; what is gone is left out, never guessed at, and named in one
 * sentence the page shows.
 *
 * - A table the dataset no longer holds takes with it everything that belongs to that table: its set-ups, the node its
 *   rows were filtered to and its search. The arrangement is the dataset's, and stays.
 * - A table the dataset still lists but the reader cannot open (the viewer's `unreadable`: a table the catalog lists to
 *   them and refuses them the rows of, or one with no version to read) is left out the same way, and named with the
 *   dataset's own reason, never as gone: the viewer leaves such a table out of `tables`.
 * - A set-up naming a column its table no longer has is left out, with the node drawn from it.
 *
 * Pure, so each branch is testable without a browser.
 */
import { missingColumns, type SetupPanel, type Setups } from '#lib/plain/setup.ts';
import type { ViewSnapshot } from './store.ts';

/** What of a view is applied, and what it names that is gone, as sentences; none when nothing is. */
export interface FittedView {
	readonly view: ViewSnapshot;
	readonly missing: readonly string[];
}

const PANELS: readonly SetupPanel[] = ['atlas', 'graph', 'tree'];

const listed = (names: readonly string[]): string =>
	names.length === 1 ? `the column ${names[0]}` : `the columns ${names.join(', ')}`;

/**
 * `view` with what `tables` (each table the reader can open, by name, and its columns) does not hold left out; a table
 * in `unreadable` (each table the dataset lists that the reader cannot open, and why) is named with its reason. A view
 * that names no table (a stamped dataset's) has nothing to hold against them.
 */
export function fitView(
	view: ViewSnapshot,
	tables: Readonly<Record<string, readonly string[]>>,
	unreadable: Readonly<Record<string, string>> = {},
): FittedView {
	if (view.table === null) return { view, missing: [] };
	const columns = tables[view.table];
	if (columns === undefined) {
		const why = Object.hasOwn(unreadable, view.table) ? unreadable[view.table] : undefined;
		return {
			view: { ...view, table: null, setups: {}, node: null, q: null },
			missing: [
				why === undefined
					? `This view names the table ${view.table}, which this dataset no longer holds: its set-ups and search were left out.`
					: `This view names the table ${view.table}, which you cannot read here (${why}): its set-ups and search were left out.`,
			],
		};
	}
	const setups: Setups = {};
	const missing: string[] = [];
	for (const panel of PANELS) {
		const setup = view.setups[panel];
		if (setup === undefined) continue;
		const gone = missingColumns(setup, columns);
		if (gone.length === 0) Object.assign(setups, { [panel]: setup });
		else
			missing.push(
				`This view's ${panel} set-up names ${listed(gone)}, which the table ${view.table} no longer has: it was left out.`,
			);
	}
	const node = view.node !== null && view.node.of in setups ? view.node : null;
	return { view: { ...view, setups, node }, missing };
}
