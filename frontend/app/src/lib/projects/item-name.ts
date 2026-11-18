/**
 * What an item is called where a person reads it (the canvas's name and breadcrumb, the queue's item column, the quick
 * view): its document's title and where in it the item is, read off the dataset's own declaration of its rows, never
 * its key.
 */
import { formatClock } from '#lib/media/time/timeline.ts';

/** The parts of a dataset's view the name is read through (`DatasetView`). */
export interface NamingView {
	title(row: Record<string, unknown>): string;
	time(row: Record<string, unknown>): { start: number; end: number } | null;
	caption(row: Record<string, unknown>): string | null;
	/** The row's identity, the document's key first (`identity.key_fields`). */
	readonly keyFields: readonly string[];
}

/**
 * The item's name: its document's title, then the stretch of the recording it is, else the row's caption (a page's own
 * words for where it is) where that says more than the title, else its place in the document (its last key, counted
 * from one), so two rows of one document (two paragraphs of a letter) are never called alike.
 */
export function itemName(view: NamingView, row: Record<string, unknown>): string {
	const title = view.title(row);
	const time = view.time(row);
	if (time) return `${title}, ${formatClock(time.start)}–${formatClock(time.end)}`;
	const caption = view.caption(row);
	if (caption && caption !== title) return caption;
	const place = view.keyFields.length > 1 ? row[view.keyFields.at(-1) ?? ''] : undefined;
	return typeof place === 'number' && Number.isInteger(place)
		? `${title} · part ${place + 1}`
		: title;
}
