/**
 * What a recording's time strip draws, as data, and the rectangles it is drawn with. The strip (`TimeStrip.svelte`) is a
 * ruler, the waveform and any number of rows of spans and markers (speakers, segments, a labeling layer's regions, a
 * track's keyframes), all through one `TimeView`; this module lays them out and turns them into rectangles for
 * `#lib/gpu/rects.ts`. Pure apart from the colours, which the caller resolves.
 */
import { RECT_FLOATS } from '#lib/gpu/rects.ts';
import { columnsFor, type Waveform } from './waveform/peaks.ts';
import { xOf, type TimeView } from './view.ts';

export type Rgba = readonly [number, number, number, number];

/** A stretch on a row. */
export interface StripSpan {
	key: string;
	start: number;
	end: number;
	colour: Rgba;
	label?: string;
	selected?: boolean;
	/** Drawn as an outline only: a stretch nobody marked yet (an unlabelled region) reads as unfinished. */
	hollow?: boolean;
}

/** A moment on a row: a track's keyframe. */
export interface StripMarker {
	key: string;
	t: number;
	colour: Rgba;
	selected?: boolean;
}

export interface StripRow {
	id: string;
	label: string;
	height: number;
	spans: readonly StripSpan[];
	markers?: readonly StripMarker[];
}

export const RULER = 18;
/** The waveform lane's height when a strip names none. */
export const WAVE = 64;

/** Each row's top, from the strip's top: the ruler, the waveform (`wave` pixels tall, 0 for none), then the rows. */
export function layout(rows: readonly StripRow[], wave: number): { top: number; row: StripRow }[] {
	let top = RULER + wave;
	return rows.map((row) => {
		const at = { top, row };
		top += row.height;
		return at;
	});
}

export const stripHeight = (rows: readonly StripRow[], wave: number): number =>
	RULER + wave + rows.reduce((h, r) => h + r.height, 0);

/** The ruler's ticks: a step from a list a clock reads well in, the smallest at least `minPx` apart. */
export function ticks(view: TimeView, width: number, minPx = 72): number[] {
	const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
	const span = view.end - view.start;
	const step = steps.find((s) => (s / span) * width >= minPx) ?? 3600;
	const out: number[] = [];
	for (let t = Math.ceil(view.start / step) * step; t <= view.end + 1e-9; t += step)
		out.push(Number(t.toFixed(3)));
	return out;
}

/** The palette a strip draws with, each a theme token resolved to RGBA. */
export interface StripColours {
	wave: Rgba;
	waveCut: Rgba;
	lane: Rgba;
	tick: Rgba;
	playhead: Rgba;
	selection: Rgba;
}

/** The strip as rectangles (`RECT_FLOATS` floats each), back to front. */
export function stripRects(opts: {
	view: TimeView;
	width: number;
	rows: readonly StripRow[];
	waveform: Waveform | null;
	/** The waveform lane's height; 0 for none. */
	wave: number;
	playhead: number;
	colours: StripColours;
}): { rects: Float32Array; count: number } {
	const { view, width, rows, waveform, wave, playhead, colours } = opts;
	const out: number[] = [];
	const push = (x0: number, y0: number, x1: number, y1: number, c: Rgba) => {
		const a = Math.max(0, Math.min(width, Math.min(x0, x1)));
		const b = Math.max(0, Math.min(width, Math.max(x0, x1)));
		if (b - a <= 0 || y1 <= y0) return;
		out.push(a, y0, b, y1, c[0], c[1], c[2], c[3]);
	};
	const x = (t: number) => xOf(view, t, width);
	for (const t of ticks(view, width)) push(x(t) - 0.5, RULER - 6, x(t) + 0.5, RULER, colours.tick);
	if (wave > 0) {
		const mid = RULER + wave / 2;
		const amp = wave / 2 - 3;
		push(0, mid - 0.5, width, mid + 0.5, colours.lane);
		if (waveform && waveform.audio) {
			const columns = Math.max(1, Math.floor(width));
			const values = columnsFor(waveform, view.start, view.end, columns);
			for (let c = 0; c < columns; c += 1) {
				const lo = values[c * 2] ?? Number.NaN;
				const hi = values[c * 2 + 1] ?? Number.NaN;
				if (Number.isNaN(lo) || Number.isNaN(hi)) continue;
				const y0 = mid - Math.max(hi, 0.02) * amp;
				const y1 = mid - Math.min(lo, -0.02) * amp;
				push(c, y0, c + 1, y1, colours.wave);
			}
			// What the decode did not reach is shaded, so a cut waveform never reads as silence.
			if (waveform.covered < waveform.duration)
				push(x(waveform.covered), RULER, x(waveform.duration), RULER + wave, colours.waveCut);
		}
	}
	for (const { top, row } of layout(rows, wave)) {
		push(0, top + row.height - 1, width, top + row.height, colours.lane);
		for (const s of row.spans) {
			if (s.end < view.start || s.start > view.end) continue;
			const y0 = top + 2;
			const y1 = top + row.height - 3;
			const [r, g, b, a] = s.colour;
			if (!s.hollow) push(x(s.start), y0, x(s.end), y1, [r, g, b, a * (s.selected ? 0.75 : 0.45)]);
			const edge: Rgba = s.selected ? colours.selection : [r, g, b, 1];
			const w = s.selected ? 2 : 1;
			push(x(s.start), y0, x(s.start) + w, y1, edge);
			push(x(s.end) - w, y0, x(s.end), y1, edge);
			if (s.hollow || s.selected) {
				push(x(s.start), y0, x(s.end), y0 + w, edge);
				push(x(s.start), y1 - w, x(s.end), y1, edge);
			}
		}
		for (const m of row.markers ?? []) {
			if (m.t < view.start || m.t > view.end) continue;
			const cx = x(m.t);
			const cy = top + row.height / 2;
			const r = m.selected ? 6 : 4.5;
			// A diamond, in three bands: wide in the middle, narrower above and below.
			const c = m.selected ? colours.selection : m.colour;
			push(cx - r, cy - r / 3, cx + r, cy + r / 3, c);
			push(cx - (r * 2) / 3, cy - (r * 2) / 3, cx + (r * 2) / 3, cy + (r * 2) / 3, c);
			push(cx - r / 3, cy - r, cx + r / 3, cy + r, c);
		}
	}
	const px = x(playhead);
	push(px - 1, 0, px + 1, stripHeight(rows, wave), colours.playhead);
	return { rects: new Float32Array(out), count: out.length / RECT_FLOATS };
}

/** What a pointer at (`x`, `y`) is over: the ruler, the waveform, a row (and on it a span, near which of its edges, or
 *  a marker). */
export type StripHit =
	| { area: 'ruler' | 'wave'; t: number }
	| {
			area: 'row';
			t: number;
			row: string;
			span: StripSpan | null;
			edge: 'start' | 'end' | null;
			marker: StripMarker | null;
	  };

export function hitTest(opts: {
	view: TimeView;
	width: number;
	rows: readonly StripRow[];
	wave: number;
	x: number;
	y: number;
	/** How near an edge, in pixels, a press takes the edge rather than the span. */
	grab?: number;
}): StripHit {
	const { view, width, rows, wave, x, y, grab = 6 } = opts;
	const t = view.start + (x / width) * (view.end - view.start);
	if (y < RULER) return { area: 'ruler', t };
	if (y < RULER + wave) return { area: 'wave', t };
	for (const { top, row } of layout(rows, wave)) {
		if (y < top || y >= top + row.height) continue;
		const marker =
			(row.markers ?? []).find((m) => Math.abs(xOf(view, m.t, width) - x) <= grab) ?? null;
		const near = (s: StripSpan) =>
			Math.min(Math.abs(xOf(view, s.start, width) - x), Math.abs(xOf(view, s.end, width) - x));
		const inside = row.spans.filter(
			(s) => xOf(view, s.start, width) - grab <= x && x <= xOf(view, s.end, width) + grab,
		);
		const span = inside.sort((a, b) => near(a) - near(b))[0] ?? null;
		let edge: 'start' | 'end' | null = null;
		if (span) {
			const ds = Math.abs(xOf(view, span.start, width) - x);
			const de = Math.abs(xOf(view, span.end, width) - x);
			if (Math.min(ds, de) <= grab) edge = ds <= de ? 'start' : 'end';
		}
		return { area: 'row', t, row: row.id, span, edge, marker };
	}
	return { area: 'ruler', t };
}
