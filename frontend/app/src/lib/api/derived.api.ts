/**
 * A set-up's reads of a table nobody stamped, from the viewer's derived doors as the person
 * (`viewer.api.v1.endpoints.derived_views`): a table's profile, which the set-up forms suggest from, and the
 * Atlas's points, the Graph's nodes and edges and the Tree's hierarchy the picked columns draw. Each answer
 * says how much of the table it read, whether that was a sample, and what it cut.
 *
 * A set-up form asks only the check doors, which read the profile's sample of the table, never the whole
 * view (`graphCheck`, `treeCheck`); a node's rows come from the door that counted them (`nodeRows`), by the
 * node's own key, so the Rows panel lists exactly the rows the node stands for.
 */
import * as v from 'valibot';
import { tableFromIPC, type Vector } from 'apache-arrow';
import { query } from '#lib/reads.svelte.ts';
import { readBytes, readJSON } from './client.ts';
import { orFail, RowsPage } from './datasets.api.ts';
import { serviceUrl } from './services.ts';
import type { AtlasSetup, GraphSetup, TreeSetup } from '#lib/plain/setup.ts';
import { setupQuery } from '#lib/plain/setup.ts';

const Extent = v.object({
	total: v.number(),
	read: v.number(),
	sampled: v.boolean(),
	cut: v.nullable(v.string()),
});
export type Extent = v.InferOutput<typeof Extent>;

const ProfiledColumn = v.object({
	name: v.string(),
	kind: v.string(),
	dim: v.nullable(v.number()),
	distinct: v.nullable(v.number()),
	distinct_capped: v.boolean(),
	nulls: v.number(),
	separator: v.nullable(v.string()),
	separated: v.number(),
	examples: v.array(v.string()),
	refers_to: v.record(v.string(), v.number()),
});

const TableProfile = v.object({
	table: v.string(),
	version: v.number(),
	extent: Extent,
	columns: v.array(ProfiledColumn),
});
export type TableProfile = v.InferOutput<typeof TableProfile>;

/** A graph's node: `id` is its typed key (`i:7` for an integer, `s:7` for text), which its rows are read
 *  by; `rows` is how many rows name it, `first` the first of them. */
const GraphNode = v.object({
	id: v.string(),
	name: v.string(),
	type: v.string(),
	mentions: v.number(),
	rows: v.number(),
	first: v.number(),
});

const GraphDiagnostics = v.object({
	extent: Extent,
	nodes: v.number(),
	edges: v.number(),
	null_rows: v.number(),
	dropped_weights: v.number(),
	self_loops: v.number(),
	unmatched_targets: v.number(),
	mixed_types: v.boolean(),
	clipped_keys: v.number(),
	cut_nodes: v.number(),
	cut_edges: v.number(),
});
export type GraphDiagnostics = v.InferOutput<typeof GraphDiagnostics>;

const DerivedGraph = v.object({
	built: v.boolean(),
	nodes: v.array(GraphNode),
	edges: v.array(v.object({ source: v.string(), target: v.string(), description: v.string() })),
	diagnostics: GraphDiagnostics,
});
export type DerivedGraph = v.InferOutput<typeof DerivedGraph>;

/** A tree's node: `id` is its opaque key (absent on a "more" leaf), `rows` the rows at or below it. */
export interface DerivedTreeNode {
	name: string;
	id?: string | undefined;
	value?: number | undefined;
	rows: number;
	first?: number | undefined;
	children?: DerivedTreeNode[] | undefined;
}

const TreeNode: v.GenericSchema<DerivedTreeNode> = v.lazy(() =>
	v.object({
		name: v.string(),
		id: v.optional(v.string()),
		value: v.optional(v.number()),
		rows: v.number(),
		first: v.optional(v.number()),
		children: v.optional(v.array(TreeNode)),
	}),
);

const TreeDiagnostics = v.object({
	extent: Extent,
	nodes: v.number(),
	roots: v.number(),
	orphans: v.number(),
	cycles: v.number(),
	null_rows: v.number(),
	dropped_sizes: v.number(),
	clipped: v.number(),
	depth: v.number(),
	cut_nodes: v.number(),
});
export type TreeDiagnostics = v.InferOutput<typeof TreeDiagnostics>;

const DerivedTree = v.object({
	built: v.boolean(),
	hierarchy: TreeNode,
	noise_label: v.string(),
	diagnostics: TreeDiagnostics,
});
export type DerivedTree = v.InferOutput<typeof DerivedTree>;

const NodeRows = v.object({ ...RowsPage.entries, extent: Extent });
export type NodeRows = v.InferOutput<typeof NodeRows>;

/** Whether a tree node's key names the rows at it alone (its "(itself)" leaf) rather than at or below it. */
export function aloneKey(key: string): boolean {
	try {
		const parsed: unknown = JSON.parse(key);
		return (
			typeof parsed === 'object' && parsed !== null && 'alone' in parsed && parsed.alone === true
		);
	} catch {
		return false;
	}
}

/** The atlas's points decoded: float16 bits for the GPU, their float32 values for picking, each point's row. */
export interface DerivedPoints {
	count: number;
	extent: Extent;
	/** Rows read and left out: a position (or vector) that was no finite number. */
	dropped: number;
	method: 'columns' | 'pca';
	/** The share of the variance a projection keeps; null for picked coordinates. */
	variance: number | null;
	x: Float32Array;
	y: Float32Array;
	xBits: Uint16Array;
	yBits: Uint16Array;
	rowid: Float64Array;
	color:
		| { kind: 'category'; codes: Int32Array; labels: string[] }
		| { kind: 'number'; values: Float32Array }
		| null;
	label: { codes: Int32Array; labels: string[] } | null;
}

interface TableArgs {
	dataset: string;
	table: string;
	version: number;
}

const tablePath = ({ dataset, table }: TableArgs, read: string): string =>
	`/api/datasets/${encodeURIComponent(dataset)}/tables/${encodeURIComponent(table)}/${read}`;

function withVersion(params: URLSearchParams, version: number): string {
	params.set('version', String(version));
	return params.toString();
}

export const tableProfile = query('unchecked', async (args: TableArgs) => {
	const url = serviceUrl(
		'viewer',
		`${tablePath(args, 'profile')}?${withVersion(new URLSearchParams(), args.version)}`,
	);
	return orFail(await readJSON(url, TableProfile, 'the viewer'));
});

export const derivedGraph = query('unchecked', async (args: TableArgs & { setup: GraphSetup }) => {
	const url = serviceUrl(
		'viewer',
		`${tablePath(args, 'graph')}?${withVersion(setupQuery(args.setup), args.version)}`,
	);
	return orFail(await readJSON(url, DerivedGraph, 'the viewer'));
});

export const derivedTree = query('unchecked', async (args: TableArgs & { setup: TreeSetup }) => {
	const url = serviceUrl(
		'viewer',
		`${tablePath(args, 'tree')}?${withVersion(setupQuery(args.setup), args.version)}`,
	);
	return orFail(await readJSON(url, DerivedTree, 'the viewer'));
});

/** What a graph set-up's columns hold in the profile's sample alone: the form's diagnostics. */
export const graphCheck = query('unchecked', async (args: TableArgs & { setup: GraphSetup }) => {
	const url = serviceUrl(
		'viewer',
		`${tablePath(args, 'graph/check')}?${withVersion(setupQuery(args.setup), args.version)}`,
	);
	return orFail(await readJSON(url, GraphDiagnostics, 'the viewer'));
});

/** What a tree set-up's columns hold in the profile's sample alone: the form's diagnostics. */
export const treeCheck = query('unchecked', async (args: TableArgs & { setup: TreeSetup }) => {
	const url = serviceUrl(
		'viewer',
		`${tablePath(args, 'tree/check')}?${withVersion(setupQuery(args.setup), args.version)}`,
	);
	return orFail(await readJSON(url, TreeDiagnostics, 'the viewer'));
});

/** The node a Rows panel lists: which panel's, its key, and the set-up it was drawn from. */
export type NodePick =
	| { of: 'graph'; key: string; setup: GraphSetup }
	| { of: 'tree'; key: string; setup: TreeSetup };

/** One page of the rows a graph's or a tree's node stands for, decided by the door that counted them. */
export const nodeRows = query(
	'unchecked',
	async (args: TableArgs & { node: NodePick; offset: number; limit: number }) => {
		const params = setupQuery(args.node.setup);
		params.set('key', args.node.key);
		params.set('offset', String(args.offset));
		params.set('limit', String(args.limit));
		const url = serviceUrl(
			'viewer',
			`${tablePath(args, `${args.node.of}/rows`)}?${withVersion(params, args.version)}`,
		);
		return orFail(await readJSON(url, NodeRows, 'the viewer'));
	},
);

export const derivedPoints = query('unchecked', async (args: TableArgs & { setup: AtlasSetup }) => {
	const url = serviceUrl(
		'viewer',
		`${tablePath(args, 'atlas')}?${withVersion(setupQuery(args.setup), args.version)}`,
	);
	return decodePoints(orFail(await readBytes(url, 'the viewer')).bytes);
});

function float16(bits: Uint16Array): Float32Array {
	const out = new Float32Array(bits.length);
	for (let i = 0; i < bits.length; i++) {
		const h = bits[i]!;
		const exponent = (h & 0x7c00) >> 10;
		const fraction = (h & 0x03ff) / 1024;
		const sign = h & 0x8000 ? -1 : 1;
		out[i] =
			exponent === 0x1f
				? sign * (fraction ? Number.NaN : Infinity)
				: exponent === 0
					? sign * 6.103515625e-5 * fraction
					: sign * 2 ** (exponent - 15) * (1 + fraction);
	}
	return out;
}

function dictionary(vector: Vector | null): { codes: Int32Array; labels: string[] } | null {
	if (!vector) return null;
	const codes = new Int32Array(vector.length);
	let labels: string[] = [];
	let at = 0;
	for (const data of vector.data) {
		if (data.dictionary && labels.length === 0) labels = data.dictionary.toArray() as string[];
		codes.set((data.values as Int32Array).subarray(0, data.length), at);
		at += data.length;
	}
	return { codes, labels };
}

/** The atlas door's Arrow IPC stream, read; a payload missing what the map draws is refused. */
function decodePoints(bytes: Uint8Array): DerivedPoints {
	const table = tableFromIPC(bytes);
	const meta = table.schema.metadata;
	const xBits = (table.getChild('x')?.toArray() ?? new Uint16Array()) as Uint16Array;
	const yBits = (table.getChild('y')?.toArray() ?? new Uint16Array()) as Uint16Array;
	const rowids = table.getChild('rowid');
	const count = Number(meta.get('count') ?? table.numRows);
	if (xBits.length !== count || yBits.length !== count || !rowids || rowids.length !== count) {
		throw new Error('the atlas answer does not hold one position and row per point');
	}
	const rowid = new Float64Array(count);
	let i = 0;
	for (const id of rowids) rowid[i++] = Number(id);
	const colour = table.getChild('color');
	const colourKind = meta.get('color_kind');
	const variance = meta.get('variance');
	return {
		count,
		extent: {
			total: Number(meta.get('total') ?? count),
			read: Number(meta.get('read') ?? count),
			sampled: meta.get('sampled') === 'true',
			cut: meta.get('cut') ?? null,
		},
		dropped: Number(meta.get('dropped') ?? 0),
		method: meta.get('method') === 'pca' ? 'pca' : 'columns',
		variance: variance === undefined ? null : Number(variance),
		x: float16(xBits),
		y: float16(yBits),
		xBits,
		yBits,
		rowid,
		color:
			colour && colourKind === 'number'
				? { kind: 'number', values: colour.toArray() as Float32Array }
				: colour
					? { kind: 'category', ...dictionary(colour)! }
					: null,
		label: dictionary(table.getChild('label')),
	};
}
