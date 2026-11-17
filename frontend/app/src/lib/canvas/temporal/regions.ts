/**
 * Regions of a recording as the waveform's labeling layer handles them: which lane each sits on when they overlap, where
 * a dragged edge snaps to, and which region is next. Pure.
 */

/** A region: its row on the canvas and its stretch in seconds. */
export interface Region {
	index: number;
	start: number;
	end: number;
}

/**
 * Each region's lane: the first lane free when it starts, so overlapping regions sit one above the other and a region
 * that does not overlap any goes on the top lane. In order of start, then of end, so the result is the same whatever
 * order the rows come in.
 */
export function laneOf(regions: readonly Region[]): Map<number, number> {
	const sorted = [...regions].sort(
		(a, b) => a.start - b.start || a.end - b.end || a.index - b.index,
	);
	const laneEnds: number[] = [];
	const out = new Map<number, number>();
	for (const r of sorted) {
		let lane = laneEnds.findIndex((end) => end <= r.start);
		if (lane < 0) {
			lane = laneEnds.length;
			laneEnds.push(r.end);
		} else laneEnds[lane] = r.end;
		out.set(r.index, lane);
	}
	return out;
}

/** `t` moved onto the nearest of `targets` within `tolerance` seconds, or `t` unchanged. */
export function snapTo(t: number, targets: readonly number[], tolerance: number): number {
	let best = t;
	let gap = tolerance;
	for (const target of targets) {
		const d = Math.abs(target - t);
		if (d <= gap) {
			gap = d;
			best = target;
		}
	}
	return best;
}

/** Where an edge of `moving` may snap: the playhead and every other region's edges. */
export function snapTargets(
	regions: readonly Region[],
	moving: number | null,
	playhead: number,
): number[] {
	const out = [playhead];
	for (const r of regions) if (r.index !== moving) out.push(r.start, r.end);
	return out;
}

/** The region after (`1`) or before (`-1`) `current` in order of start; the first or last when none is current. */
export function neighbour(
	regions: readonly Region[],
	current: number | null,
	direction: 1 | -1,
): Region | null {
	const sorted = [...regions].sort(
		(a, b) => a.start - b.start || a.end - b.end || a.index - b.index,
	);
	if (sorted.length === 0) return null;
	const at = sorted.findIndex((r) => r.index === current);
	if (at < 0) return (direction === 1 ? sorted[0] : sorted[sorted.length - 1]) ?? null;
	return sorted[at + direction] ?? null;
}
