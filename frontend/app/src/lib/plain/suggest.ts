/**
 * What the set-up forms offer for each role, from a table's profile (the viewer's `.../profile`: each
 * column's kind and what a sample says of it), and what they fill in before the person confirms.
 *
 * A suggestion is a candidate with the reason it is offered, ranked; nothing is drawn from it until the
 * person confirms the form. A role is FILLED IN only where exactly one candidate is offered for it: one
 * vector column for the atlas's position, one pair of columns named as coordinates, one low-cardinality
 * text column for a colour. Two plausible answers are a choice the person makes, never a guess applied.
 *
 * Pure, so the ranking and the rule for filling in are testable without a browser.
 */
import type { GraphSetup } from './setup.ts';

/** One column as the viewer's profile describes it. */
export interface ProfiledColumn {
	name: string;
	kind: string;
	dim?: number | null;
	distinct?: number | null;
	distinct_capped?: boolean;
	nulls?: number;
	separator?: string | null;
	separated?: number;
	refers_to?: Record<string, number>;
}

export interface Profile {
	extent: { read: number; total: number; sampled: boolean };
	columns: ProfiledColumn[];
}

export interface Candidate<T> {
	value: T;
	/** Why it is offered, in the words the form shows beside it. */
	reason: string;
}

export type AtlasPlace =
	| { place: 'columns'; x: string; y: string }
	| { place: 'vector'; vector: string };
export type GraphLinks = Pick<GraphSetup, 'shape' | 'source' | 'target'>;
export type TreeNesting =
	| { shape: 'parent'; node: string; parent: string }
	| { shape: 'path'; node: string; separator: string };

export interface Suggestions {
	atlas: { place: Candidate<AtlasPlace>[]; color: Candidate<string>[]; label: Candidate<string>[] };
	graph: {
		links: Candidate<GraphLinks>[];
		relation: Candidate<string>[];
		weight: Candidate<string>[];
		label: Candidate<string>[];
		group: Candidate<string>[];
	};
	tree: {
		nesting: Candidate<TreeNesting>[];
		label: Candidate<string>[];
		size: Candidate<string>[];
	};
}

const NUMERIC = new Set(['integer', 'float', 'decimal']);
const KEYS = new Set(['text', 'integer']);
/** A colour reads at most this many categories at a glance. */
const FEW = 24;

/** Name pairs that say "the two coordinates of a point", each with what they say. */
const COORDINATES: readonly { a: RegExp; b: RegExp; what: string; stem: boolean }[] = [
	{ a: /^(.*?)[_-]?x$/, b: /^(.*?)[_-]?y$/, what: 'x and y', stem: true },
	{
		a: /^(.*?)[_-]?(1|0)$/,
		b: /^(.*?)[_-]?(2|1)$/,
		what: 'a first and second component',
		stem: true,
	},
	{
		a: /^(lon|lng|longitude)$/,
		b: /^(lat|latitude)$/,
		what: 'longitude and latitude',
		stem: false,
	},
];
/** Hints that a pair of numbers is a projection someone already computed. */
const PROJECTION = /umap|tsne|t_sne|pca|proj|embed/;

/** The two ends of an edge as a column names them, after a stem the pair shares (`shipped_from`, `shipped_to`). */
const EDGE_FROM = /^(.*?)_?(src|source|from|head|subject|start|origin|sender)(_id)?$/;
const EDGE_TO = /^(.*?)_?(dst|dest|target|to|tail|object|end|destination|receiver)(_id)?$/;
const REFERENCE =
	/^(parent|parent_id|points_to|ref|refs|broader|reports_to|manager|manager_id|up|of)$/;
const PATH_SEPARATORS = new Set(['/', ' > ', '>', '\\']);
const PATH_NAME = /path|place|location|category|taxonomy|hierarchy|breadcrumb|folder|collection/;
/** A column named as a row's own key. */
const IDENTIFIER = /(^|_)(id|key|uuid)$/;
const LABEL = /^(name|title|label|text|note|caption|description|summary)$/;
const WEIGHT = /^(weight|count|n|score|strength|value|amount|w)$/;

/** A column whose every row holds a value of its own: a key. */
function unique(column: ProfiledColumn, read: number): boolean {
	return (
		read > 0 &&
		(column.nulls ?? 0) === 0 &&
		(column.distinct_capped === true || column.distinct === read)
	);
}

function few(column: ProfiledColumn): boolean {
	return (
		column.distinct_capped !== true &&
		column.distinct !== null &&
		column.distinct !== undefined &&
		column.distinct >= 2 &&
		column.distinct <= FEW
	);
}

function fewReason(column: ProfiledColumn, sampled: boolean): string {
	return `${column.distinct} distinct values${sampled ? ' in a sample' : ''}`;
}

/** Text or boolean columns with few distinct values, fewest first, as colours or groups. */
function categories(profile: Profile, kinds: Set<string>): Candidate<string>[] {
	return profile.columns
		.filter((c) => kinds.has(c.kind) && few(c))
		.sort((a, b) => (a.distinct ?? 0) - (b.distinct ?? 0) || a.name.localeCompare(b.name))
		.map((c) => ({ value: c.name, reason: fewReason(c, profile.extent.sampled) }));
}

/** Text columns that name each row: named as a label, or nearly unique. */
function labels(profile: Profile): Candidate<string>[] {
	const read = profile.extent.read;
	return profile.columns
		.filter(
			(c) =>
				c.kind === 'text' &&
				!IDENTIFIER.test(c.name.toLowerCase()) &&
				(LABEL.test(c.name.toLowerCase()) || unique(c, read)),
		)
		.sort(
			(a, b) => Number(LABEL.test(b.name.toLowerCase())) - Number(LABEL.test(a.name.toLowerCase())),
		)
		.map((c) => ({
			value: c.name,
			reason: LABEL.test(c.name.toLowerCase())
				? 'named as a label'
				: `a different value on every row${profile.extent.sampled ? ' sampled' : ''}`,
		}));
}

function places(profile: Profile): Candidate<AtlasPlace>[] {
	const out: Candidate<AtlasPlace>[] = [];
	for (const c of profile.columns) {
		if (c.kind === 'vector' && (c.dim ?? 0) >= 2) {
			out.push({
				value: { place: 'vector', vector: c.name },
				reason: `a vector of ${c.dim} values, projected to two dimensions`,
			});
		}
	}
	const numbers = profile.columns.filter((c) => NUMERIC.has(c.kind)).map((c) => c.name);
	for (const x of numbers) {
		for (const y of numbers) {
			if (x === y) continue;
			const lx = x.toLowerCase();
			const ly = y.toLowerCase();
			for (const pair of COORDINATES) {
				const mx = pair.a.exec(lx);
				const my = pair.b.exec(ly);
				// A pair shares its stem (`umap_x` with `umap_y`), or has none (`x`, `y`, `lon`, `lat`).
				if (!mx || !my || (pair.stem && (mx[1] ?? '') !== (my[1] ?? ''))) continue;
				if (pair.what === 'a first and second component' && Number(mx[2]) + 1 !== Number(my[2]))
					continue;
				const stem = pair.stem ? (mx[1] ?? '') : '';
				out.push({
					value: { place: 'columns', x, y },
					reason: PROJECTION.test(stem)
						? `named as a ${stem.match(PROJECTION)?.[0]} projection (${pair.what})`
						: `named as ${pair.what}`,
				});
			}
		}
	}
	return out;
}

function references(
	profile: Profile,
): { id: ProfiledColumn; ref: ProfiledColumn; reason: string }[] {
	const read = profile.extent.read;
	const ids = profile.columns.filter((c) => KEYS.has(c.kind) && unique(c, read));
	const out: { id: ProfiledColumn; ref: ProfiledColumn; reason: string }[] = [];
	for (const id of ids) {
		for (const ref of profile.columns) {
			if (ref.name === id.name || ref.kind !== id.kind) continue;
			// A column named as a reference refers to a column named as a key; any other must be seen to.
			const named =
				REFERENCE.test(ref.name.toLowerCase()) && IDENTIFIER.test(id.name.toLowerCase());
			const found = ref.refers_to?.[id.name] ?? 0;
			if (!named && found < 0.5) continue;
			const share = `${Math.round(found * 100)}% of its values are ${id.name} values${profile.extent.sampled ? ' in a sample' : ''}`;
			out.push({ id, ref, reason: named ? `named as a reference; ${share}` : share });
		}
	}
	return out;
}

function links(profile: Profile): Candidate<GraphLinks>[] {
	const out: Candidate<GraphLinks>[] = [];
	const keys = profile.columns.filter((c) => KEYS.has(c.kind));
	for (const a of keys) {
		for (const b of keys) {
			if (a.name === b.name || a.kind !== b.kind) continue;
			const from = EDGE_FROM.exec(a.name.toLowerCase());
			const to = EDGE_TO.exec(b.name.toLowerCase());
			if (from && to && from[1] === to[1]) {
				out.push({
					value: { shape: 'edges', source: a.name, target: b.name },
					reason: 'named as the two ends of an edge',
				});
			}
		}
	}
	for (const { id, ref, reason } of references(profile)) {
		out.push({
			value: { shape: 'self', source: id.name, target: ref.name },
			reason: `each row points to another: ${reason}`,
		});
	}
	return out;
}

function nestings(profile: Profile): Candidate<TreeNesting>[] {
	const out: Candidate<TreeNesting>[] = [];
	for (const c of profile.columns) {
		if (c.kind !== 'text' || !c.separator || (c.separated ?? 0) < 0.5) continue;
		// A slash or an arrow is a path's own separator; free text holds a colon or a full stop as often, so those
		// count only in a column named as a path.
		if (!PATH_SEPARATORS.has(c.separator) && !PATH_NAME.test(c.name.toLowerCase())) continue;
		out.push({
			value: { shape: 'path', node: c.name, separator: c.separator },
			reason: `${Math.round((c.separated ?? 0) * 100)}% of its values hold “${c.separator}”`,
		});
	}
	for (const { id, ref, reason } of references(profile)) {
		out.push({ value: { shape: 'parent', node: id.name, parent: ref.name }, reason });
	}
	return out;
}

export function suggest(profile: Profile): Suggestions {
	const numbers = profile.columns.filter((c) => NUMERIC.has(c.kind));
	const weights = numbers
		.filter((c) => WEIGHT.test(c.name.toLowerCase()))
		.map((c) => ({ value: c.name, reason: 'named as a weight' }));
	const sizes = numbers
		.filter(
			(c) =>
				WEIGHT.test(c.name.toLowerCase()) || /size|bytes|length|pages/.test(c.name.toLowerCase()),
		)
		.map((c) => ({ value: c.name, reason: 'named as an amount' }));
	return {
		atlas: {
			place: places(profile),
			color: categories(profile, new Set(['text', 'boolean', 'integer'])),
			label: labels(profile),
		},
		graph: {
			links: links(profile),
			relation: categories(profile, new Set(['text'])).map((c) => ({
				...c,
				reason: `${c.reason}: a kind of link`,
			})),
			weight: weights,
			label: labels(profile),
			group: categories(profile, new Set(['text', 'boolean'])),
		},
		tree: {
			nesting: nestings(profile),
			label: labels(profile),
			size: sizes,
		},
	};
}

/** A form's fields filled in before the person confirms: a column name per role, or none. */
export type Prefill = Record<string, string | undefined>;

/** The candidate a role is filled in with: the only one offered, or none. */
function only<T>(candidates: readonly Candidate<T>[]): T | undefined {
	return candidates.length === 1 ? candidates[0]!.value : undefined;
}

/** The atlas set-up the form opens with: the only position offered, and the only colour and label. */
export function prefillAtlas(s: Suggestions): Prefill {
	const place = only(s.atlas.place);
	return { ...place, color: only(s.atlas.color), label: only(s.atlas.label) };
}

export function prefillGraph(s: Suggestions): Prefill {
	const linked = only(s.graph.links);
	// A label names the node a row is: a self-referencing table's row, never an edge table's "from".
	return {
		...linked,
		weight: only(s.graph.weight),
		label: linked?.shape === 'self' ? only(s.graph.label) : undefined,
	};
}

export function prefillTree(s: Suggestions): Prefill {
	const nesting = only(s.tree.nesting);
	return {
		...nesting,
		label: nesting?.shape === 'parent' ? only(s.tree.label) : undefined,
	};
}
