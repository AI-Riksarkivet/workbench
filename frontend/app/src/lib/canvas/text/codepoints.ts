/**
 * A span's offsets are CODE POINTS, on the wire and in the canvas: the annotator measures and slices a row's text by
 * code point (Python's `str`), and a character outside the Basic Multilingual Plane (a fraktur initial, an emoji) is one
 * code point and two UTF-16 units. The browser's strings and its DOM count UTF-16 units, so every offset crossing from
 * a JavaScript string or a DOM position into a span goes through here, and a position inside a surrogate pair is
 * rounded down to the character's start, so no span ever splits one.
 */

/** How many code points `text` holds. */
export function pointLength(text: string): number {
	let n = 0;
	for (const _ of text) n += 1;
	return n;
}

/** The code points `start`..`end` of `text`. */
export function pointSlice(text: string, start: number, end: number): string {
	return text.slice(toUnits(text, start), toUnits(text, end));
}

/** The UTF-16 unit at which code point `points` of `text` starts (the text's length past its end). */
export function toUnits(text: string, points: number): number {
	let units = 0;
	let n = 0;
	for (const ch of text) {
		if (n >= points) return units;
		units += ch.length;
		n += 1;
	}
	return units;
}

/** The code point at UTF-16 unit `units` of `text`; a unit inside a surrogate pair counts as its character's start. */
export function toPoints(text: string, units: number): number {
	let at = 0;
	let n = 0;
	for (const ch of text) {
		if (at + ch.length > units) return n;
		at += ch.length;
		n += 1;
	}
	return n;
}

/** The code point at `points` of `text`, or the empty string past its end. */
export function pointAt(text: string, points: number): string {
	return pointSlice(text, points, points + 1);
}
