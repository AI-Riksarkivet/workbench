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
	/** The row whose text a text span ranges into; empty for every other row. */
	parentId?: string;
	/** The row's status: `prediction` is a model's proposal nobody kept or removed yet. */
	status?: string;
}

/** Whether a row marks the item and so takes a class: a region, or a text span (a `text` row naming a parent). A text
 *  that names none is the item's own content (its text, a typed question), as the annotator's submit reads it. */
const marks = (r: CheckedRow): boolean =>
	REGION_SHAPES.has(r.shape) || (r.shape === 'text' && Boolean(r.parentId));

/** What keeps an item from being submitted, in the words the panel shows, with the rows to point at. */
export interface SubmitBlockers {
	/** Regions and text spans that carry no class, while the task's ontology names classes. */
	unlabelled: number[];
	/** Required classes no row carries. */
	missing: string[];
	/** The ontology names classes and no row was drawn, which it refuses unless it allows an empty item. */
	empty: boolean;
	/** How many proposals nobody kept or removed: a submission is a person's, so each is settled first (ADR 0019),
	 *  whatever the ontology names. */
	proposals: number;
}

/** The transitions an item holding a proposal nobody kept or removed is refused (`machines.SETTLES_PROPOSALS`). */
export const SETTLES_PROPOSALS: ReadonlySet<string> = new Set([
	'submit',
	'accept',
	'fix_and_accept',
]);

/** Why an item holding ``count`` proposals nobody kept or removed is not submitted (``event`` `submit`) or accepted
 *  (`accept`, `fix_and_accept`), in the annotator's own words (`annotator.projects.machines.unsettled_proposals`). */
export function unsettledProposals(count: number, event: string): string {
	const doing = event === 'submit' ? 'submitting' : 'accepting';
	return count === 1
		? `1 proposal on this item is neither kept nor removed: keep or remove it before ${doing}`
		: `${count} proposals on this item are neither kept nor removed: keep or remove each before ${doing}`;
}

/** Whether a row would be published as a label: neither turned down (`rejected`) nor a proposal nobody settled
 *  (`prediction`), the one rule the annotator's submit and publish share (`projects.publish.NOT_A_LABEL`). */
const isLabel = (r: CheckedRow): boolean => r.status !== 'rejected' && r.status !== 'prediction';

/** The checks the annotator's submit applies first, read early so the panel can name what to fix
 *  (`annotator.projects.ontology.validate_against_ontology`); the submit still decides. The contract is judged on the
 *  rows that would be published: a rejected row is left out of every check, and a proposal is counted only as one. */
export function submitBlockers(
	classes: readonly ClassDef[],
	rows: readonly CheckedRow[],
	allowEmpty: boolean,
): SubmitBlockers {
	const proposals = rows.filter((r) => r.status === 'prediction').length;
	if (classes.length === 0) return { unlabelled: [], missing: [], empty: false, proposals };
	const labels = rows.filter(isLabel);
	const present = new Set(labels.map((r) => r.label).filter(Boolean));
	return {
		unlabelled: labels.filter((r) => marks(r) && !r.label).map((r) => r.index),
		missing: classes.filter((c) => c.required && !present.has(c.name)).map((c) => c.name),
		empty: labels.length === 0 && !allowEmpty,
		proposals,
	};
}

/** Whether nothing blocks the submit. */
export const submittable = (b: SubmitBlockers): boolean =>
	b.unlabelled.length === 0 && b.missing.length === 0 && !b.empty && b.proposals === 0;

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
