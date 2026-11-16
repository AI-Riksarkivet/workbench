/**
 * A document's parts as the viewer's `/api/doc-chunks` answers them: its rows in order (its pages, or a recording's
 * segments), each carrying the key fields AFTER the document key and not the document key itself, which the
 * document's id already says.
 */
import type { Row } from '@workbench/explorer-api';
import { SvelteMap } from 'svelte/reactivity';

/** The part of `DatasetView` this reads, declared structurally so it needs no descriptor. */
export interface PartKeys {
	readonly keyFields: string[];
	readonly docKeyField: string;
}

/** Where `row` is among its document's `parts`, matched on the key fields after the document key; -1 if absent. */
export function partIndexOf(view: PartKeys, parts: readonly Row[], row: Row): number {
	const keys = view.keyFields.filter((k) => k !== view.docKeyField);
	if (keys.length === 0) return -1;
	return parts.findIndex((part) => keys.every((k) => String(part[k]) === String(row[k])));
}

/**
 * The page a reader turned to in each opened row, for the session: a viewer mounts again when the dock moves or
 * maximizes its panel, and the reader stays where they were. Keyed by the dataset and the opened row.
 */
export const turnedTo = new SvelteMap<string, number>();
