/**
 * A person's set-up of the Atlas, Graph and Tree for a table nobody stamped: which columns feed each, as a
 * value the panels read, a view saves and a link carries. Nothing here reads or writes the table; the
 * viewer's derived doors (`viewer.api.v1.endpoints.derived_views`) take a set-up as their query.
 *
 * A set-up names columns only. One the table no longer holds is reported by the panel (`missingColumns`),
 * never sent; one read from a link or a stored view is validated here first (`readSetups`), and what does
 * not validate, or is longer than a link should carry, is dropped with the reason, never executed.
 *
 * Pure, so the codec and its bounds are testable without a browser.
 */
import * as v from 'valibot';

/** The longest column name a set-up names (the viewer's own bound). */
const MAX_COLUMN = 256;
/** The most characters a link's set-up parameters may hold together; past it the set-up is ignored. */
const MAX_SETUP_CHARS = 2048;

const Column = v.pipe(v.string(), v.minLength(1), v.maxLength(MAX_COLUMN));
const Optional = v.optional(Column);

const AtlasSetup = v.variant('place', [
	v.object({
		place: v.literal('columns'),
		x: Column,
		y: Column,
		color: Optional,
		label: Optional,
	}),
	v.object({ place: v.literal('vector'), vector: Column, color: Optional, label: Optional }),
]);
export type AtlasSetup = v.InferOutput<typeof AtlasSetup>;

const GraphSetup = v.object({
	shape: v.picklist(['edges', 'self']),
	source: Column,
	target: Column,
	relation: Optional,
	weight: Optional,
	label: Optional,
	group: Optional,
});
export type GraphSetup = v.InferOutput<typeof GraphSetup>;

const TreeSetup = v.variant('shape', [
	v.object({
		shape: v.literal('parent'),
		node: Column,
		parent: Column,
		label: Optional,
		size: Optional,
	}),
	v.object({
		shape: v.literal('path'),
		node: Column,
		separator: v.pipe(v.string(), v.minLength(1), v.maxLength(8)),
		size: Optional,
	}),
]);
export type TreeSetup = v.InferOutput<typeof TreeSetup>;

export const Setups = v.object({
	atlas: v.optional(AtlasSetup),
	graph: v.optional(GraphSetup),
	tree: v.optional(TreeSetup),
});
export type Setups = v.InferOutput<typeof Setups>;

export type SetupPanel = 'atlas' | 'graph' | 'tree';

/** `value` as set-ups, or why it is not: a stored view's set-ups are read through here, as a link's are. */
export function parseSetups(value: unknown): { setups: Setups } | { problem: string } {
	const parsed = v.safeParse(Setups, value);
	if (parsed.success) return { setups: parsed.output };
	return { problem: parsed.issues.map((issue) => issue.message).join('; ') };
}

/** Every column a set-up names, in its form's order. */
function columnsOf(setup: AtlasSetup | GraphSetup | TreeSetup): string[] {
	const named: (string | undefined)[] =
		'place' in setup
			? setup.place === 'columns'
				? [setup.x, setup.y, setup.color, setup.label]
				: [setup.vector, setup.color, setup.label]
			: 'source' in setup
				? [setup.source, setup.target, setup.relation, setup.weight, setup.label, setup.group]
				: setup.shape === 'parent'
					? [setup.node, setup.parent, setup.label, setup.size]
					: [setup.node, setup.size];
	return named.filter((name): name is string => name !== undefined);
}

/** The columns a set-up names that the table no longer holds: the panel says so and offers to set up again. */
export function missingColumns(
	setup: AtlasSetup | GraphSetup | TreeSetup,
	columns: readonly string[],
): string[] {
	const held = new Set(columns);
	return columnsOf(setup).filter((name) => !held.has(name));
}

/** The query a derived door reads `setup` with. */
export function setupQuery(setup: AtlasSetup | GraphSetup | TreeSetup): URLSearchParams {
	const params = new URLSearchParams();
	for (const [key, value] of Object.entries(setup)) {
		if (key === 'place' || value === undefined) continue;
		params.set(key, String(value));
	}
	return params;
}

/**
 * The link's parameter for each field, kept short and readable (`ax=lon&ay=lat`). Each panel's
 * discriminant comes first, so a reader can tell the shape before the columns.
 */
const ATLAS_KEYS = { x: 'ax', y: 'ay', vector: 'av', color: 'ac', label: 'al' } as const;
const GRAPH_KEYS = {
	shape: 'gs',
	source: 'gf',
	target: 'gt',
	relation: 'gr',
	weight: 'gw',
	label: 'gl',
	group: 'gg',
} as const;
const TREE_KEYS = {
	shape: 'ts',
	node: 'tn',
	parent: 'tp',
	separator: 'tsep',
	label: 'tl',
	size: 'tz',
} as const;

/** Every parameter the set-ups own in a link. */
export const SETUP_PARAMS: readonly string[] = [
	...Object.values(ATLAS_KEYS),
	...Object.values(GRAPH_KEYS),
	...Object.values(TREE_KEYS),
];

type Params = Pick<URLSearchParams, 'get'>;

function picked<K extends string>(
	params: Params,
	keys: Record<K, string>,
): Partial<Record<K, string>> {
	const out: Partial<Record<K, string>> = {};
	for (const [field, name] of Object.entries(keys) as [K, string][]) {
		const value = params.get(name);
		if (value !== null) out[field] = value;
	}
	return out;
}

/**
 * The set-ups a link carries, validated; one that does not validate, or set-up parameters longer than
 * {@link MAX_SETUP_CHARS}, is left out and `problem` says why, so the page can tell the person rather
 * than draw what it cannot trust.
 */
export function readSetups(params: Params & Pick<URLSearchParams, 'has'>): {
	setups: Setups;
	problem: string | null;
} {
	const size = SETUP_PARAMS.reduce((sum, name) => sum + (params.get(name)?.length ?? 0), 0);
	if (size === 0) return { setups: {}, problem: null };
	if (size > MAX_SETUP_CHARS) {
		return {
			setups: {},
			problem: `its set-up is longer than ${MAX_SETUP_CHARS} characters, so it was not used`,
		};
	}
	const setups: Setups = {};
	const problems: string[] = [];
	const atlas = picked(params, ATLAS_KEYS);
	if (Object.keys(atlas).length > 0) {
		const parsed = v.safeParse(AtlasSetup, {
			...atlas,
			place: atlas.vector !== undefined ? 'vector' : 'columns',
		});
		if (parsed.success) setups.atlas = parsed.output;
		else problems.push('the atlas set-up is not one the workbench can read');
	}
	const graph = picked(params, GRAPH_KEYS);
	if (Object.keys(graph).length > 0) {
		const parsed = v.safeParse(GraphSetup, graph);
		if (parsed.success) setups.graph = parsed.output;
		else problems.push('the graph set-up is not one the workbench can read');
	}
	const tree = picked(params, TREE_KEYS);
	if (Object.keys(tree).length > 0) {
		const parsed = v.safeParse(TreeSetup, tree);
		if (parsed.success) setups.tree = parsed.output;
		else problems.push('the tree set-up is not one the workbench can read');
	}
	return {
		setups,
		problem: problems.length > 0 ? `${problems.join('; ')}, so it was not used` : null,
	};
}

/** `out` with the set-ups' parameters written (each panel's, where it is set up). */
export function writeSetups(out: URLSearchParams, setups: Setups): void {
	const write = (
		keys: Record<string, string>,
		value: Record<string, unknown> | undefined,
	): void => {
		if (value === undefined) return;
		for (const [field, name] of Object.entries(keys)) {
			const item = value[field];
			if (typeof item === 'string' && item !== '') out.set(name, item);
		}
	};
	write(ATLAS_KEYS, setups.atlas);
	write(GRAPH_KEYS, setups.graph);
	write(TREE_KEYS, setups.tree);
}
