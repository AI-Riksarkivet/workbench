/**
 * A task's classes as the canvas offers them: which classes a drawn region may take, each one's colour, and what still
 * keeps the item from being submitted. Pure, so the rules are pinned without a canvas.
 *
 * The classes come from the task's captured ontology, never from the labels already on the item: an item nobody has
 * labelled yet has no labels, and a picker built from them offers nothing.
 */

/** One class of the task's ontology, as the canvas needs it. */
export interface ClassDef {
	name: string;
	/** The ontology's own colour (`#rrggbb`), or null to take one from the palette. */
	colour: string | null;
	/** The shapes it may be drawn with; empty is any. */
	tools: readonly string[];
	required: boolean;
}

/** The shapes that mark a region of the item. A `tag` is a whole-item choice and a `text` its own content (a span is a
 *  range into another row's text), so neither is a region that takes a class from the picker. */
const REGION_SHAPES: ReadonlySet<string> = new Set([
	'bbox',
	'polygon',
	'mask',
	'keypoint',
	'polyline',
	'segment',
]);

/** Colours a class without one of its own takes, by its place in the ontology: distinct hues that read on the page's
 *  light and dark grounds alike. */
const PALETTE = [
	'#e6550d',
	'#3182bd',
	'#31a354',
	'#c51b8a',
	'#8c6d31',
	'#756bb1',
	'#17becf',
	'#d62728',
	'#636363',
	'#bcbd22',
];

/** The classes a region drawn with `shape` may take: those whose tools name it or name nothing, in the ontology's
 *  order, every tag-only and text-only class left out. */
export function classesFor(classes: readonly ClassDef[], shape: string): ClassDef[] {
	return classes.filter((c) => c.tools.length === 0 || c.tools.includes(shape));
}

/** The classes any region may take: those that may be drawn with a region shape. */
export function regionClasses(classes: readonly ClassDef[]): ClassDef[] {
	return classes.filter(
		(c) => c.tools.length === 0 || c.tools.some((tool) => REGION_SHAPES.has(tool)),
	);
}

/** Each class's colour: its own, else the palette's at its place among the classes. */
export function classColours(classes: readonly ClassDef[]): Map<string, string> {
	return new Map(
		classes.map((c, i) => [c.name, c.colour ?? PALETTE[i % PALETTE.length] ?? '#636363']),
	);
}

/** One row as the submit check reads it. */
export interface CheckedRow {
	index: number;
	shape: string;
	label: string;
}

/** What keeps an item from being submitted, in the words the panel shows, with the rows to point at. */
export interface SubmitBlockers {
	/** Regions that carry no class, while the task's ontology names classes. */
	unlabelled: number[];
	/** Required classes no row carries. */
	missing: string[];
	/** The ontology names classes and no row was drawn, which it refuses unless it allows an empty item. */
	empty: boolean;
}

/** The checks the annotator's submit applies first, read early so the panel can name what to fix
 *  (`annotator.projects.ontology.validate_against_ontology`); the submit still decides. */
export function submitBlockers(
	classes: readonly ClassDef[],
	rows: readonly CheckedRow[],
	allowEmpty: boolean,
): SubmitBlockers {
	if (classes.length === 0) return { unlabelled: [], missing: [], empty: false };
	const present = new Set(rows.map((r) => r.label).filter(Boolean));
	return {
		unlabelled: rows.filter((r) => REGION_SHAPES.has(r.shape) && !r.label).map((r) => r.index),
		missing: classes.filter((c) => c.required && !present.has(c.name)).map((c) => c.name),
		empty: rows.length === 0 && !allowEmpty,
	};
}

/** Whether nothing blocks the submit. */
export const submittable = (b: SubmitBlockers): boolean =>
	b.unlabelled.length === 0 && b.missing.length === 0 && !b.empty;

/** The class a key picks: `1`..`9` the first nine, in order; null for any other key or one past the classes. */
export function classForKey(classes: readonly ClassDef[], key: string): ClassDef | null {
	if (!/^[1-9]$/.test(key)) return null;
	return classes[Number(key) - 1] ?? null;
}

/** The classes whose name starts with `typed` (any case), then those that contain it: the picker's type-ahead. */
export function matchClasses(classes: readonly ClassDef[], typed: string): ClassDef[] {
	const q = typed.trim().toLowerCase();
	if (!q) return [...classes];
	const starts = classes.filter((c) => c.name.toLowerCase().startsWith(q));
	const contains = classes.filter(
		(c) => !c.name.toLowerCase().startsWith(q) && c.name.toLowerCase().includes(q),
	);
	return [...starts, ...contains];
}
