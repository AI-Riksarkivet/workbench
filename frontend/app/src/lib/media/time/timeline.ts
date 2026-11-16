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

/**
 * Where time sits on a horizontal axis drawn over a recording's length, for a layer over the scrubber (a
 * labeling layer's spans): a time's fraction of the width, and the time at a fraction or a pointer's x.
 */
export interface TimeAxis {
	readonly duration: number;
	/** `t`'s place along the axis, from 0 at its start to 1 at its end. */
	fractionOf(t: number): number;
	/** The time at a place along the axis. */
	timeAt(fraction: number): number;
	/** The time under a pointer's `clientX`, read off the axis as it is laid out now. */
	timeAtClientX(clientX: number): number;
}

/** The axis of a recording `duration` long, laid out where `box()` says. */
export function timeAxis(duration: number, box: () => DOMRect | null): TimeAxis {
	const clamp = (f: number) => Math.min(1, Math.max(0, f));
	return {
		duration,
		fractionOf: (t) => (duration > 0 ? clamp(t / duration) : 0),
		timeAt: (fraction) => clamp(fraction) * duration,
		timeAtClientX(clientX) {
			const rect = box();
			if (!rect || rect.width <= 0) return 0;
			return clamp((clientX - rect.left) / rect.width) * duration;
		},
	};
}
