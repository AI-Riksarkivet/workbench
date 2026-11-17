import { describe, expect, it } from 'vitest';
import { boxAt, presence, tracksFrom, type TrackRow } from './tracks.ts';

const kf = (id: string, t: number, x: number, over: Partial<TrackRow> = {}): TrackRow => ({
	id,
	index: Number(id.slice(1)),
	group: 'trk-a',
	tStart: t,
	x,
	y: 0,
	width: 10,
	height: 10,
	label: 'cart',
	shape: 'bbox',
	...over,
});

/** Half a frame at 25 fps, the tolerance the canvas passes. */
const HALF = 0.02;

describe('a track', () => {
	const [track] = tracksFrom([
		kf('k2', 2, 40),
		kf('k0', 0, 0),
		kf('k4', 4, 40, { width: 0, height: 0 }),
		kf('k6', 6, 100),
	]);

	it('reads the rows of one group as its keyframes, in time order, ignoring a box with no time and other shapes', () => {
		const tracks = tracksFrom([
			kf('k0', 0, 0),
			kf('k1', 1, 0, { tStart: null }),
			kf('k2', 2, 0, { shape: 'polygon' }),
		]);
		expect(tracks.map((t) => t.keyframes.map((k) => k.id))).toEqual([['k0']]);
		expect(track?.keyframes.map((k) => k.t)).toEqual([0, 2, 4, 6]);
	});

	it('is interpolated linearly between two keyframes and held after its last', () => {
		expect(track && boxAt(track, 1, HALF)).toEqual({ x: 20, y: 0, width: 10, height: 10 });
		expect(track && boxAt(track, 7, HALF)).toEqual({ x: 100, y: 0, width: 10, height: 10 });
	});

	it('is absent before its first keyframe and from an outside keyframe to the next box, holding still toward it', () => {
		expect(track && boxAt(track, -0.5, HALF)).toBeNull();
		expect(track && boxAt(track, 3, HALF)).toEqual({ x: 40, y: 0, width: 10, height: 10 });
		expect(track && boxAt(track, 5, HALF)).toBeNull();
	});

	it('is present on the frame of its first keyframe, whose time float32 rounded up', () => {
		// The table stores a keyframe's time as float32; the playhead is a double. Every frame at 25 fps whose float32
		// rounding is above it (k / 25 for k = 1, 3, 4, ...) must still find its own keyframe.
		const rounded = Array.from({ length: 150 }, (_, k) => k / 25).filter((t) => Math.fround(t) > t);
		expect(rounded.length).toBeGreaterThan(50);
		for (const t of rounded) {
			const [one] = tracksFrom([kf('k0', Math.fround(t), 30)]);
			expect(one && boxAt(one, t, HALF), `frame at ${t}`).toEqual({
				x: 30,
				y: 0,
				width: 10,
				height: 10,
			});
		}
		// With a keyframe after it, the box on that frame is the keyframe's own, not one extrapolated before it.
		const [t0 = 0, t1 = 1] = rounded;
		const [two] = tracksFrom([kf('k0', Math.fround(t0), 0), kf('k1', Math.fround(t1), 80)]);
		expect(two && boxAt(two, t0, HALF)).toEqual({ x: 0, y: 0, width: 10, height: 10 });
	});

	it('is present on its lane from each keyframe with a box to the next outside one', () => {
		expect(track && presence(track, 8)).toEqual([
			{ start: 0, end: 4 },
			{ start: 6, end: 8 },
		]);
	});
});
