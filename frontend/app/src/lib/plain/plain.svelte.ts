/**
 * The plain table a dataset with no workbench descriptor is shown as: which of its tables is in view, the
 * page asked of it (offset, sort, a text search, a similar-rows ranking, or the rows a node names), the row
 * opened, and the person's set-ups of its Atlas, Graph and Tree (`./setup.ts`). The dock's panels read and
 * write it here, not in component state, since the dock remounts a panel whenever it is moved, maximized
 * or popped out (`#lib/dock/Dock.svelte`'s panel rule).
 *
 * A set-up belongs to the table in view: choosing another table starts it unset. The node picked in the
 * Graph or drilled in the Tree lists, in the Rows panel, the rows the viewer counted for it: the panel asks
 * the node's own door by the node's key and the set-up it was drawn from (`#lib/api/derived.api.ts`'s
 * `nodeRows`), never a name search, so the rows listed are the rows the node's count counted.
 */
import type { DatasetFacts, RowsQuery } from '#lib/api/datasets.api.ts';
import type { NodePick } from '#lib/api/derived.api.ts';
import type { LinkedNode } from '#lib/dock/link.ts';
import type { AtlasSetup, GraphSetup, SetupPanel, Setups, TreeSetup } from './setup.ts';

const PAGE_ROWS = 50;

const FIRST_PAGE: RowsQuery = {
	offset: 0,
	limit: PAGE_ROWS,
	sort: null,
	order: 'asc',
	q: null,
	near: null,
};

/** The node `node` names in `setups`' graph or tree, or null when that panel is not set up. */
function nodePick(setups: Setups, node: LinkedNode): NodePick | null {
	if (node.of === 'graph')
		return setups.graph ? { of: 'graph', key: node.id, setup: setups.graph } : null;
	return setups.tree ? { of: 'tree', key: node.id, setup: setups.tree } : null;
}

/** A node's name read from its key alone, for a node a view or a link restores before its panel is drawn: a
 *  graph key's value, a tree key's last level or id. */
function keyName(of: 'graph' | 'tree', key: string): string {
	if (of === 'graph') return key.slice(2);
	try {
		const parsed: unknown = JSON.parse(key);
		if (typeof parsed === 'object' && parsed !== null) {
			if ('path' in parsed && Array.isArray(parsed.path)) return String(parsed.path.at(-1) ?? key);
			if ('id' in parsed && typeof parsed.id === 'string') return parsed.id.slice(2);
		}
	} catch {
		// A key no tree door answered with: shown as it is.
	}
	return key;
}

const CLOSED: Record<SetupPanel, boolean> = { atlas: false, graph: false, tree: false };

class PlainTable {
	facts = $state.raw<DatasetFacts | null>(null);
	table = $state<string | null>(null);
	rows = $state<RowsQuery>({ ...FIRST_PAGE });
	/** The row open in the Row panel, with the table version its page was read at. */
	opened = $state.raw<{ table: string; rowid: number; version: number; qualified: string } | null>(
		null,
	);
	/** The person's set-ups of the table in view. */
	setups = $state.raw<Setups>({});
	/** The panels whose set-up form is open over what they draw. */
	editing = $state.raw<Record<SetupPanel, boolean>>(CLOSED);
	/** The node whose rows the Rows panel lists, the set-up it was drawn from, and the name it is shown by. */
	node = $state.raw<(NodePick & { name: string }) | null>(null);

	/** Show `facts`' dataset afresh: `table` (or its first), the first page, no row open, nothing set up. */
	open(facts: DatasetFacts, table: string | null = null): void {
		this.facts = facts;
		const tables = Object.keys(facts.tables).sort();
		this.table = table !== null && tables.includes(table) ? table : (tables[0] ?? null);
		this.missing = table !== null && !tables.includes(table) ? table : null;
		this.rows = { ...FIRST_PAGE };
		this.opened = null;
		this.setups = {};
		this.editing = CLOSED;
		this.node = null;
	}

	/** A table a view or a link named that this dataset does not hold; shown, never waited for. */
	missing = $state<string | null>(null);

	/** Show `table`'s rows, nothing set up; a table the dataset does not hold is reported and not chosen.
	 *  Returns whether it was chosen. */
	choose(table: string): boolean {
		if (this.facts !== null && !(table in this.facts.tables)) {
			this.missing = table;
			return false;
		}
		this.missing = null;
		this.table = table;
		this.rows = { ...FIRST_PAGE };
		this.opened = null;
		this.setups = {};
		this.editing = CLOSED;
		this.node = null;
		return true;
	}

	search(q: string): void {
		this.node = null;
		this.rows = { ...FIRST_PAGE, q: q.trim() || null };
	}

	similarTo(rowid: number): void {
		this.node = null;
		this.rows = { ...FIRST_PAGE, near: rowid };
	}

	/** Sort by `column`: ascending first, then descending, then the table's own order. A node's rows keep the
	 *  table's order, so its Rows panel offers no sort. */
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

	/** Back to the table's own order, every search, ranking and node filter dropped. */
	reset(): void {
		this.node = null;
		this.rows = { ...FIRST_PAGE };
	}

	turn(offset: number): void {
		this.rows = { ...this.rows, offset: Math.max(0, offset) };
	}

	/** Apply a panel's confirmed set-up: its form closes, and a node its old set-up filtered by is dropped. */
	setUp(panel: SetupPanel, setup: AtlasSetup | GraphSetup | TreeSetup): void {
		this.setups = { ...this.setups, [panel]: setup };
		this.editing = { ...this.editing, [panel]: false };
		if (this.node?.of === panel) this.reset();
	}

	/** Take a view's or a link's set-ups, and the node it filtered by, as they were saved. */
	restore(setups: Setups, node: LinkedNode | null): void {
		this.setups = setups;
		this.editing = CLOSED;
		if (node !== null) this.pickNode(node.of, node.id, keyName(node.of, node.id));
		else if (this.node !== null) this.reset();
	}

	edit(panel: SetupPanel, open = true): void {
		this.editing = { ...this.editing, [panel]: open };
	}

	/** List the rows the node keyed `id` stands for in the Rows panel, in its panel's set-up. */
	pickNode(of: 'graph' | 'tree', id: string, name: string): void {
		const pick = nodePick(this.setups, { of, id });
		if (pick === null) return;
		this.node = { ...pick, name };
		this.rows = { ...FIRST_PAGE };
	}
}

export const plain = new PlainTable();
