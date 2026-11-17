/**
 * Two versions of one item's annotations, side by side: which shape of one is which shape of the other, and how they
 * differ. A version is the rows of one annotator's task (a consensus item's replicas), or a model's proposals beside a
 * person's shapes on the same item. Pure, so the matching rules are pinned without a canvas.
 *
 * Shapes are matched within their kind: a picture's regions by how much their boxes overlap, a recording's regions by
 * how much their stretches of time overlap, text spans by how much their characters overlap within the same text,
 * whole-item choices by their class. Each shape is matched at most once, best overlap first, and only at an overlap
 * of at least {@link MATCH}: below it they are two shapes, one in each version.
 */

/** What the comparison reads of a shape. */
export interface CompareShape {
	/** The row's index in its own version's table. */
	index: number;
	id: string;
	shape: string;
	label: string;
	/** A region of the picture: its bounding box. */
	box: { x: number; y: number; w: number; h: number } | null;
	/** A region of time, in seconds. */
	time: { start: number; end: number } | null;
	/** A text span: the text it ranges into and its characters. */
	span: { text: string; start: number; end: number } | null;
}

/** How a pair stands: alike, the same shape differing (place or class), or a shape only one version has. */
export type DiffKind = 'same' | 'changed' | 'mine' | 'theirs';

export interface DiffEntry {
	kind: DiffKind;
	mine: CompareShape | null;
	theirs: CompareShape | null;
	/** How much the pair overlaps, 0 to 1; 0 for a shape with no partner. */
	overlap: number;
}

/** The overlap at which two shapes are one shape placed differently. */
const MATCH = 0.5;
/** The overlap at which a matched pair with one class is alike. */
const SAME = 0.9;

type Family = 'box' | 'time' | 'span' | 'tag';

function familyOf(s: CompareShape): Family | null {
	if (s.shape === 'tag') return 'tag';
	if (s.span) return 'span';
	if (s.shape === 'segment' && s.time) return 'time';
	if (s.box) return 'box';
	return null;
}

function ranges(a0: number, a1: number, b0: number, b1: number): number {
	const inter = Math.max(0, Math.min(a1, b1) - Math.max(a0, b0));
	const union = Math.max(a1, b1) - Math.min(a0, b0);
	return union > 0 ? inter / union : a0 === b0 && a1 === b1 ? 1 : 0;
}

/** How much two shapes of one kind overlap (intersection over union), 0 to 1. */
function overlap(a: CompareShape, b: CompareShape): number {
	const family = familyOf(a);
	if (family === null || family !== familyOf(b)) return 0;
	if (family === 'tag') return a.label === b.label ? 1 : 0;
	if (family === 'time' && a.time && b.time)
		return ranges(a.time.start, a.time.end, b.time.start, b.time.end);
	if (family === 'span' && a.span && b.span)
		return a.span.text === b.span.text
			? ranges(a.span.start, a.span.end, b.span.start, b.span.end)
			: 0;
	if (family === 'box' && a.box && b.box) {
		const ix = Math.max(
			0,
			Math.min(a.box.x + a.box.w, b.box.x + b.box.w) - Math.max(a.box.x, b.box.x),
		);
		const iy = Math.max(
			0,
			Math.min(a.box.y + a.box.h, b.box.y + b.box.h) - Math.max(a.box.y, b.box.y),
		);
		const inter = ix * iy;
		const union = a.box.w * a.box.h + b.box.w * b.box.h - inter;
		return union > 0 ? inter / union : 0;
	}
	return 0;
}

/** Every shape of both versions, paired where they match, in the order of `mine` then the shapes only `theirs` has. */
export function diffVersions(
	mine: readonly CompareShape[],
	theirs: readonly CompareShape[],
): DiffEntry[] {
	const candidates: { m: number; t: number; o: number }[] = [];
	mine.forEach((a, m) =>
		theirs.forEach((b, t) => {
			const o = overlap(a, b);
			if (o >= MATCH) candidates.push({ m, t, o });
		}),
	);
	candidates.sort((x, y) => y.o - x.o || x.m - y.m || x.t - y.t);
	const pairOfMine = new Map<number, { t: number; o: number }>();
	const takenTheirs = new Set<number>();
	for (const c of candidates) {
		if (pairOfMine.has(c.m) || takenTheirs.has(c.t)) continue;
		pairOfMine.set(c.m, { t: c.t, o: c.o });
		takenTheirs.add(c.t);
	}
	const out: DiffEntry[] = mine.map((a, m) => {
		const pair = pairOfMine.get(m);
		const b = pair ? (theirs[pair.t] ?? null) : null;
		if (!pair || !b) return { kind: 'mine', mine: a, theirs: null, overlap: 0 };
		const kind: DiffKind = a.label === b.label && pair.o >= SAME ? 'same' : 'changed';
		return { kind, mine: a, theirs: b, overlap: pair.o };
	});
	theirs.forEach((b, t) => {
		if (!takenTheirs.has(t)) out.push({ kind: 'theirs', mine: null, theirs: b, overlap: 0 });
	});
	return out;
}
