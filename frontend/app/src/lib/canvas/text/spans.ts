/**
 * Text spans as the text canvas draws them: which rows are the item's lines of text, in reading order; which lane each
 * span sits on where spans overlap; and the stretches of a line between span edges, each with the spans covering it.
 * Pure, so the rules are pinned without a page.
 *
 * A span is a row naming a parent row (`parentId`) and a range of characters in its text (`[charStart, charEnd)`):
 * offsets, never the selected substring, since a word can repeat within a line. Offsets and lengths here are code
 * points (`./codepoints.ts`).
 */
import type { AnnoRow } from '../annotation-rows.ts';
import { laneOf } from '../temporal/regions.ts';
import { pointLength, pointSlice } from './codepoints.ts';

/** One line of text a span can range into: a row's own text, or the item's text before any row holds it. */
export interface TextLine {
	/** The row holding the text, or -1 for the item's text no row holds yet (`ensureDocument` writes one). */
	index: number;
	id: string;
	text: string;
	/** What the line is: its class, or empty for the item's own text. */
	label: string;
}

/** A span on a line, as drawn. */
export interface SpanMark {
	index: number;
	start: number;
	end: number;
	label: string;
}

/**
 * The rows whose text spans range into, in reading order: rows that name no parent and carry text (the item's text, a
 * transcribed region, a recording's transcribed segment), never a whole-item choice. Ordered by a declared reading order
 * (`order`), then by time (a recording's segments), then by height on the page (`y`), then by table order: a model
 * writes rows in whatever order its pipeline emits them, which is not the order a person reads them in.
 */
export function textLines(rows: readonly AnnoRow[]): TextLine[] {
	const by = (a: number | null, b: number | null): number =>
		a !== null && b !== null ? a - b : a !== null ? -1 : b !== null ? 1 : 0;
	return rows
		.filter((r) => r.text !== '' && r.parentId === '' && r.shape !== 'tag')
		.toSorted(
			(a, b) =>
				by(a.order, b.order) ||
				by(a.shape === 'segment' ? a.tStart : null, b.shape === 'segment' ? b.tStart : null) ||
				by(a.y, b.y) ||
				a.index - b.index,
		)
		.map((r) => ({ index: r.index, id: r.id, text: r.text, label: r.label }));
}

/** The spans of the line held by row `id`: rows naming it as their parent with a range inside its text, `length` code
 *  points long. */
export function spansOf(rows: readonly AnnoRow[], id: string, length: number): SpanMark[] {
	return rows
		.filter(
			(r) =>
				r.parentId === id &&
				r.charStart !== null &&
				r.charEnd !== null &&
				r.charStart >= 0 &&
				r.charEnd > r.charStart &&
				r.charEnd <= length,
		)
		.map((r) => ({ index: r.index, start: r.charStart ?? 0, end: r.charEnd ?? 0, label: r.label }));
}

/** Each span's lane: overlapping spans one under the other, a span that overlaps none on the first. */
export function spanLanes(spans: readonly SpanMark[]): Map<number, number> {
	return laneOf(spans);
}

/** A stretch of a line between two span edges, with the spans covering all of it. */
export interface Segment {
	start: number;
	end: number;
	text: string;
	covering: SpanMark[];
}

/** The line cut at every span edge: consecutive stretches covering the whole text, each with the spans over it. */
export function segmentText(text: string, spans: readonly SpanMark[]): Segment[] {
	const length = pointLength(text);
	const cuts = new Set([0, length]);
	for (const s of spans) {
		cuts.add(Math.max(0, Math.min(length, s.start)));
		cuts.add(Math.max(0, Math.min(length, s.end)));
	}
	const edges = [...cuts].sort((a, b) => a - b);
	const out: Segment[] = [];
	for (let i = 0; i + 1 < edges.length; i += 1) {
		const start = edges[i] ?? 0;
		const end = edges[i + 1] ?? 0;
		if (end <= start) continue;
		out.push({
			start,
			end,
			text: pointSlice(text, start, end),
			covering: spans.filter((s) => s.start <= start && s.end >= end),
		});
	}
	return out;
}

/** A span's edge moved to `at`, kept inside the line and at least one character wide; null when nothing changes. */
export function movedEdge(
	span: { start: number; end: number },
	edge: 'start' | 'end',
	at: number,
	length: number,
): { start: number; end: number } | null {
	const next =
		edge === 'start'
			? { start: Math.max(0, Math.min(at, span.end - 1)), end: span.end }
			: { start: span.start, end: Math.min(length, Math.max(at, span.start + 1)) };
	return next.start === span.start && next.end === span.end ? null : next;
}
