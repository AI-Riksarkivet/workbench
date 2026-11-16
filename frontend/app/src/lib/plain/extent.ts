/**
 * What a derived read saw, as the status line of the Atlas, the Graph, the Tree and a node's rows says it:
 * nothing when it read the whole table, else how many of the table's rows it read, and why it stopped
 * short where it did (the viewer's deadline or its byte bound, `viewer.services.derived_views`). Pure, so
 * every panel says it in the same words.
 */
import type { Extent } from '#lib/api/derived.api.ts';

export function extentNote(extent: Extent): string {
	if (!extent.sampled && extent.cut === null) return '';
	const read = `from ${extent.read.toLocaleString()} of ${extent.total.toLocaleString()} rows`;
	return extent.cut === null ? `${read}, sampled` : `${read}: ${extent.cut}`;
}
