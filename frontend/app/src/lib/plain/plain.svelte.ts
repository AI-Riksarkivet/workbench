/**
 * The plain table a dataset with no workbench descriptor is shown as: which of its tables is in view, the
 * page asked of it (offset, sort, a text search or a similar-rows ranking) and the row opened. The dock's
 * Rows and Row panels read and write it here, not in component state, since the dock remounts a panel
 * whenever it is moved, maximized or popped out (`#lib/dock/Dock.svelte`'s panel rule).
 */
import type { DatasetFacts, RowsQuery } from '#lib/api/datasets.api.ts';

const PAGE_ROWS = 50;

const FIRST_PAGE: RowsQuery = {
	offset: 0,
	limit: PAGE_ROWS,
	sort: null,
	order: 'asc',
	q: null,
	near: null,
};

class PlainTable {
	facts = $state.raw<DatasetFacts | null>(null);
	table = $state<string | null>(null);
	rows = $state<RowsQuery>({ ...FIRST_PAGE });
	/** The row open in the Row panel, with the table version its page was read at. */
	opened = $state.raw<{ table: string; rowid: number; version: number; qualified: string } | null>(
		null,
	);

	/** Show `facts`' dataset afresh: its first table, the first page, no row open. */
	open(facts: DatasetFacts): void {
		this.facts = facts;
		this.table = Object.keys(facts.tables).sort()[0] ?? null;
		this.rows = { ...FIRST_PAGE };
		this.opened = null;
	}

	choose(table: string): void {
		this.table = table;
		this.rows = { ...FIRST_PAGE };
		this.opened = null;
	}

	search(q: string): void {
		this.rows = { ...FIRST_PAGE, q: q.trim() || null };
	}

	similarTo(rowid: number): void {
		this.rows = { ...FIRST_PAGE, near: rowid };
	}

	/** Sort by `column`: ascending first, then descending, then the table's own order. */
	sortBy(column: string): void {
		const { sort, order } = this.rows;
		const next =
			sort !== column
				? { sort: column, order: 'asc' as const }
				: order === 'asc'
					? { sort: column, order: 'desc' as const }
					: { sort: null, order: 'asc' as const };
		this.rows = { ...FIRST_PAGE, ...next };
	}

	/** Back to the table's own order, every search and ranking dropped. */
	reset(): void {
		this.rows = { ...FIRST_PAGE };
	}

	turn(offset: number): void {
		this.rows = { ...this.rows, offset: Math.max(0, offset) };
	}
}

export const plain = new PlainTable();
