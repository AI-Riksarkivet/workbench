/**
 * The canvas's unsaved edits, by annotation id rather than by row index: the shape that survives a reload. A save
 * reloads the table (merge-insert renumbers its rows), and a sign-in reloads the page; in both, the edits not yet
 * saved are carried across in this shape and applied to the rows as they are now.
 *
 * Kept across a sign-in in this tab's sessionStorage under the person and the unit, so one person's edits never reach
 * another's canvas, and they stay in the browser: nothing here is sent anywhere but the annotations door, by a save.
 * A save that lands removes them, and so does signing out (`forgetPendingEdits`).
 */
import * as v from 'valibot';

const GeometrySchema = v.object({
	x: v.number(),
	y: v.number(),
	w: v.number(),
	h: v.number(),
	polygon: v.nullable(v.array(v.number())),
});

export const PendingEditsSchema = v.object({
	/** Rows drawn and not yet saved, whole. */
	inserts: v.array(v.record(v.string(), v.unknown())),
	deletes: v.array(v.string()),
	fields: v.array(v.object({ id: v.string(), field: v.string(), value: v.string() })),
	geometry: v.array(v.object({ id: v.string(), geo: GeometrySchema })),
	spans: v.array(v.object({ id: v.string(), char_start: v.number(), char_end: v.number() })),
	/** Times moved on the time axis: a region's start and end, a keyframe's moment. */
	times: v.optional(
		v.array(v.object({ id: v.string(), t_start: v.number(), t_end: v.number() })),
		[],
	),
});
export type PendingEdits = v.InferOutput<typeof PendingEditsSchema>;

export const isEmpty = (p: PendingEdits): boolean =>
	p.inserts.length +
		p.deletes.length +
		p.fields.length +
		p.geometry.length +
		p.spans.length +
		p.times.length ===
	0;

const PREFIX = 'workbench.canvas.unsaved:';

const keyOf = (person: string, unit: string): string => `${PREFIX}${person}:${unit}`;

/** Keep `edits` for `person` on `unit` until they are saved or the person signs out. */
export function keepPendingEdits(person: string, unit: string, edits: PendingEdits): void {
	try {
		sessionStorage.setItem(keyOf(person, unit), JSON.stringify(edits));
	} catch {
		// A tab that keeps nothing still has the edits on screen; it only cannot carry them across a sign-in.
	}
}

/** The edits kept for `person` on `unit`, or null. */
export function keptPendingEdits(person: string, unit: string): PendingEdits | null {
	let raw: string | null;
	try {
		raw = sessionStorage.getItem(keyOf(person, unit));
	} catch {
		return null;
	}
	if (raw === null) return null;
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return null;
	}
	const read = v.safeParse(PendingEditsSchema, parsed);
	return read.success ? read.output : null;
}

export function dropPendingEdits(person: string, unit: string): void {
	try {
		sessionStorage.removeItem(keyOf(person, unit));
	} catch {
		// Nothing was kept.
	}
}

/** Sign-out: every unit's kept edits, whoever's they were. */
export function forgetPendingEdits(): void {
	try {
		for (const key of Object.keys(sessionStorage))
			if (key.startsWith(PREFIX)) sessionStorage.removeItem(key);
	} catch {
		// Nothing was kept.
	}
}
