/**
 * OBJECT TRACKS: one object followed through a video, the way CVAT's tracks work.
 *
 * A track is a reading of rows, not a store of its own: the rows that share a `group` are one object, and each carries
 * the moment it was marked at in `t_start`, its KEYFRAME. Between two keyframes the box is interpolated, so an annotator
 * marks the object where its motion changes and the frames between are computed. The annotation schema carries all of it
 * (`group`, `t_start`, the box), so a track needs no schema change.
 *
 * LIFESPAN. A track appears at its first keyframe and stays until an OUTSIDE keyframe: one whose box has no area, which
 * says the object left the picture there. It is absent from an outside keyframe until the next keyframe with a box, and
 * after its last keyframe it stays where that keyframe put it, as CVAT's tracks do, until an outside keyframe ends it.
 * Before its first keyframe it is absent: a box there would label frames nobody marked.
 *
 * Boxes only. A polygon on a video is pinned to the one frame it was drawn on and is not interpolated: matching the
 * vertices of two polygons is a correspondence the person has not given.
 *
 * Pure: no engine, no DOM, no clock, so every rule is checked without a decoded video.
 */

/** One keyframe of a track. */
export interface Keyframe {
	id: string;
	/** Its row in the canvas's table. */
	index: number;
	group: string;
	/** The moment it marks, in seconds. */
	t: number;
	x: number;
	y: number;
	width: number;
	height: number;
	label: string;
	/** The object left the picture here: the track is absent until its next keyframe with a box. */
	outside: boolean;
}

/** A track: one object, its keyframes in time order. */
export interface Track {
	group: string;
	label: string;
	keyframes: Keyframe[];
	/** Its first and last keyframe's moments. */
	from: number;
	to: number;
}

/** A row as this reads it; structural, so a test needs no Arrow table. */
export interface TrackRow {
	id: string;
	index: number;
	group: string;
	tStart: number | null;
	x: number | null;
	y: number | null;
	width: number | null;
	height: number | null;
	label: string;
	shape: string;
}

/** The rows read as tracks: every box with a group and a time, grouped, its keyframes in time order; tracks in the
 *  order they appear, so their lanes read down the page as the video plays. */
export function tracksFrom(rows: readonly TrackRow[]): Track[] {
	const byGroup = new Map<string, Keyframe[]>();
	for (const r of rows) {
		if (!r.group || r.tStart === null || r.shape !== 'bbox') continue;
		if (r.x === null || r.y === null || r.width === null || r.height === null) continue;
		const kf: Keyframe = {
			id: r.id,
			index: r.index,
			group: r.group,
			t: r.tStart,
			x: r.x,
			y: r.y,
			width: r.width,
			height: r.height,
			label: r.label,
			outside: r.width <= 0 || r.height <= 0,
		};
		const list = byGroup.get(r.group);
		if (list) list.push(kf);
		else byGroup.set(r.group, [kf]);
	}
	const out: Track[] = [];
	for (const [group, keyframes] of byGroup) {
		keyframes.sort((a, b) => a.t - b.t || a.id.localeCompare(b.id));
		const first = keyframes[0];
		const last = keyframes[keyframes.length - 1];
		if (!first || !last) continue;
		// The label of the first keyframe with one: a track relabelled part way is one object, named once.
		const label = keyframes.find((k) => k.label)?.label ?? '';
		out.push({ group, label, keyframes, from: first.t, to: last.t });
	}
	out.sort((a, b) => a.from - b.from || a.group.localeCompare(b.group));
	return out;
}

/** A box with no identity: what a track evaluates to at a moment. */
export interface Box {
	x: number;
	y: number;
	width: number;
	height: number;
}

/**
 * Where the object is at `t`, or null where it is absent.
 *
 * A keyframe within `tolerance` of `t` counts as at `t`: a keyframe's time is stored as float32, which rounds a frame's
 * presentation time either way, so without it a track is absent on the very frame its first keyframe was drawn on. The
 * caller passes half a frame, the tolerance its keyframe lookups use.
 *
 * Linear between the keyframe at or before `t` and the next one, because an annotator corrects by adding a keyframe and
 * a smoother curve would move the box in places they did not touch. Toward an outside keyframe the box holds still: the
 * object leaves at that moment, it does not shrink into it.
 */
export function boxAt(track: Track, t: number, tolerance: number): Box | null {
	let before: Keyframe | undefined;
	let after: Keyframe | undefined;
	for (const kf of track.keyframes) {
		if (kf.t <= t + tolerance) before = kf;
		else {
			after = kf;
			break;
		}
	}
	if (!before || before.outside) return null;
	const held = { x: before.x, y: before.y, width: before.width, height: before.height };
	if (!after || after.outside || t <= before.t) return held;
	const f = (t - before.t) / (after.t - before.t);
	return {
		x: before.x + (after.x - before.x) * f,
		y: before.y + (after.y - before.y) * f,
		width: before.width + (after.width - before.width) * f,
		height: before.height + (after.height - before.height) * f,
	};
}

/** The stretches of time a track is present, for its lane: from each keyframe with a box to the next outside one, the
 *  last running to `end`. */
export function presence(track: Track, end: number): { start: number; end: number }[] {
	const out: { start: number; end: number }[] = [];
	let open: number | null = null;
	for (const kf of track.keyframes) {
		if (!kf.outside && open === null) open = kf.t;
		else if (kf.outside && open !== null) {
			out.push({ start: open, end: kf.t });
			open = null;
		}
	}
	if (open !== null) out.push({ start: open, end: Math.max(open, end) });
	return out;
}

/**
 * A fresh track id. Random rather than counted: two annotators working the same item would both mint `track-1`, and
 * their objects would merge on save, since sharing a group is what makes two rows one object.
 */
export function newTrackId(random: () => number = Math.random): string {
	return `trk-${Math.floor(random() * 0x100000000).toString(36)}`;
}
