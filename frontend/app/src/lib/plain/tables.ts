/**
 * A plain table's qualified name, as the viewer names a cell's table (`<source>~<namespace>$<table>`, or
 * `<source>~<table>` at a source's root): what a signed media item and the Row panel name a row's table by.
 */
export function qualifiedTable(dataset: string, table: string): string {
	return dataset.endsWith('~') ? `${dataset}${table}` : `${dataset}$${table}`;
}
