/**
 * The time axis a recording's viewers share: its clock, its spans (segments, transcript lines, words) and
 * which span the playhead is in. Pure, and named in no dataset's terms.
 */

/** A stretch of a recording, in seconds. */
export interface Span {
	start: number;
	end: number;
}

/** Seconds as a clock reads: `m:ss`, or `h:mm:ss` from an hour on. */
export function formatClock(seconds: number): string {
	const total = Math.max(0, Math.round(Number.isFinite(seconds) ? seconds : 0));
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const sec = total % 60;
	const pad = (n: number) => String(n).padStart(2, '0');
	return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

/**
 * The span the playhead at `t` is in: the one whose `[start, end)` holds it, else, in a gap between spans (the
 * silence between two segments), the last one that began before it, so a view that follows the playhead stays
 * where it is. -1 before the first span begins. Spans are in order of their start.
 */
export function spanAt(spans: readonly Span[], t: number): number {
	let lo = 0;
	let hi = spans.length - 1;
	let before = -1;
	while (lo <= hi) {
		const mid = (lo + hi) >> 1;
		const span = spans[mid];
		if (span === undefined) break;
		if (t < span.start) hi = mid - 1;
		else {
			before = mid;
			if (t < span.end) return mid;
			lo = mid + 1;
		}
	}
	return before;
}
