/**
 * A recording's waveform as the viewer answers it (`viewer.services.peaks`): each bucket's min and max as signed bytes,
 * `rate` buckets a second. The answer is the finest level; the coarser levels a zoomed-out view draws are built here,
 * once, by taking min and max over pairs of buckets, so drawing any view reads at most a few buckets per pixel.
 *
 * Pure: no fetch, no GPU.
 */
import * as v from 'valibot';

export const PeaksSchema = v.object({
	duration: v.number(),
	decoded: v.number(),
	rate: v.pipe(v.number(), v.minValue(0)),
	audio: v.boolean(),
	peaks: v.string(),
	cut: v.nullable(v.picklist(['bytes', 'length', 'time'])),
});
export type PeaksAnswer = v.InferOutput<typeof PeaksSchema>;

/** One level: interleaved min and max per bucket, each in [-1, 1], `rate` buckets a second. */
export interface Level {
	rate: number;
	values: Float32Array;
}

/** The waveform at every zoom: level 0 is the answer's, each next one half as many buckets. */
export interface Waveform {
	duration: number;
	/** The seconds the buckets cover; less than `duration` when the decode was cut. */
	covered: number;
	levels: Level[];
	cut: PeaksAnswer['cut'];
	audio: boolean;
}

/** The answer's buckets, as interleaved floats. */
export function decodePeaks(base64: string): Float32Array {
	const raw = atob(base64);
	const out = new Float32Array(raw.length);
	for (let i = 0; i < raw.length; i += 1) {
		const byte = raw.charCodeAt(i);
		out[i] = (byte > 127 ? byte - 256 : byte) / 127;
	}
	return out;
}

/** Half as many buckets: each the min of two mins and the max of two maxes. */
export function halve(level: Level): Level {
	const n = Math.ceil(level.values.length / 4);
	const out = new Float32Array(n * 2);
	const src = level.values;
	for (let i = 0; i < n; i += 1) {
		const a = i * 4;
		const lo = src[a + 2] === undefined ? (src[a] ?? 0) : Math.min(src[a] ?? 0, src[a + 2] ?? 0);
		const hi =
			src[a + 3] === undefined ? (src[a + 1] ?? 0) : Math.max(src[a + 1] ?? 0, src[a + 3] ?? 0);
		out[i * 2] = lo;
		out[i * 2 + 1] = hi;
	}
	return { rate: level.rate / 2, values: out };
}

/** The answer as a waveform with its levels, down to one of at most `floor` buckets. */
export function waveformOf(answer: PeaksAnswer, floor = 256): Waveform {
	const levels: Level[] = [{ rate: answer.rate, values: decodePeaks(answer.peaks) }];
	for (;;) {
		const last = levels[levels.length - 1];
		if (!last || last.values.length / 2 <= floor) break;
		levels.push(halve(last));
	}
	return {
		duration: answer.duration,
		covered: answer.decoded,
		levels,
		cut: answer.cut,
		audio: answer.audio,
	};
}

/**
 * The waveform's columns for a view of `[start, end)` seconds drawn `columns` wide: per column the min and max over the
 * buckets it spans, read from the finest level with at least one bucket a column. NaN where the column is past what the
 * buckets cover.
 */
export function columnsFor(
	waveform: Waveform,
	start: number,
	end: number,
	columns: number,
): Float32Array {
	const out = new Float32Array(columns * 2).fill(Number.NaN);
	const span = end - start;
	if (columns <= 0 || !(span > 0)) return out;
	const perColumn = span / columns;
	// The coarsest level still finer than a column, so a column spans one bucket or more and no bucket is skipped.
	let level = waveform.levels[0];
	for (const l of waveform.levels) if (1 / l.rate <= perColumn) level = l;
	if (!level) return out;
	const count = level.values.length / 2;
	for (let c = 0; c < columns; c += 1) {
		const t0 = start + c * perColumn;
		const t1 = t0 + perColumn;
		const b0 = Math.max(0, Math.floor(t0 * level.rate));
		const b1 = Math.min(count, Math.max(b0 + 1, Math.ceil(t1 * level.rate)));
		if (b0 >= count) continue;
		let lo = Infinity;
		let hi = -Infinity;
		for (let b = b0; b < b1; b += 1) {
			lo = Math.min(lo, level.values[b * 2] ?? 0);
			hi = Math.max(hi, level.values[b * 2 + 1] ?? 0);
		}
		out[c * 2] = lo;
		out[c * 2 + 1] = hi;
	}
	return out;
}
