/**
 * Span-offset remapping under a TEXT EDIT — the textual facet's integrity rule.
 *
 * A span is a `[start, end)` range into its parent's text. Correcting the transcription shifts
 * that text under every span anchored to it: without remapping, fixing one typo at the start of
 * a line silently re-points every entity after it at the wrong characters — annotations corrupted
 * by the act of improving the data they annotate.
 *
 * The edit is read as the replaced regions between the old and the new text, from a character diff
 * (jsdiff's Myers diff): a keystroke is one region, a line corrected and applied at once is several.
 * Per span, against every region:
 *
 *  - a region entirely AFTER the span → the span is untouched by it;
 *  - entirely BEFORE → the span shifts by its length delta;
 *  - a region strictly INSIDE the span → the span stretches with its text, and is dropped when nothing is left of it;
 *  - PARTIAL overlap → the span is dropped, same stance as the renderer: the anchored text no longer
 *    exists, and a clamped guess would keep a confident-looking span on characters nobody labelled.
 */
import { diffChars } from 'diff';
import { toPoints, toUnits } from './text/codepoints.ts';

export interface SpanRange {
	index: number;
	start: number;
	end: number;
}

export interface RemappedSpan {
	index: number;
	/** Null ⇒ the span's anchored text was edited away — delete it rather than guess. */
	start: number | null;
	end: number | null;
}

/** One replaced region: `[from, toOld)` of the old text became `delta` characters longer (or shorter). */
export interface EditedRegion {
	from: number;
	toOld: number;
	delta: number;
}

/** The replaced regions between two versions of a string, in order: runs of removed and added characters between the
 *  characters the two share. Equal strings have none. */
export function editedRegions(oldText: string, newText: string): EditedRegion[] {
	const regions: EditedRegion[] = [];
	let at = 0;
	let open: EditedRegion | null = null;
	for (const part of diffChars(oldText, newText)) {
		if (!part.added && !part.removed) {
			if (open) regions.push(open);
			open = null;
			at += part.value.length;
			continue;
		}
		open ??= { from: at, toOld: at, delta: 0 };
		if (part.removed) {
			open.toOld += part.value.length;
			open.delta -= part.value.length;
			at += part.value.length;
		} else open.delta += part.value.length;
	}
	if (open) regions.push(open);
	return regions;
}

/** Remap every span for the `oldText → newText` edit. Pure; the caller applies the results. Span offsets are code
 *  points (`./text/codepoints.ts`), the diff's are UTF-16 units: converted on the way in and out. */
export function remapSpans(
	oldText: string,
	newText: string,
	spans: readonly SpanRange[],
): RemappedSpan[] {
	return remapUnits(
		oldText,
		newText,
		spans.map((s) => ({
			index: s.index,
			start: toUnits(oldText, s.start),
			end: toUnits(oldText, s.end),
		})),
	).map((s) =>
		s.start === null || s.end === null
			? s
			: { index: s.index, start: toPoints(newText, s.start), end: toPoints(newText, s.end) },
	);
}

function remapUnits(oldText: string, newText: string, spans: readonly SpanRange[]): RemappedSpan[] {
	const regions = editedRegions(oldText, newText);
	return spans.map(({ index, start, end }) => {
		let shiftStart = 0;
		let shiftEnd = 0;
		for (const { from, toOld, delta } of regions) {
			if (end <= from) continue; // after the span
			if (start >= toOld) {
				// before it
				shiftStart += delta;
				shiftEnd += delta;
			} else if (start <= from && end >= toOld) shiftEnd += delta; // inside it
			else return { index, start: null, end: null }; // partially edited away — drop, never guess
		}
		// Every word of it deleted: nothing is left to mark, and an empty span is refused at submit.
		if (end + shiftEnd <= start + shiftStart) return { index, start: null, end: null };
		return { index, start: start + shiftStart, end: end + shiftEnd };
	});
}
