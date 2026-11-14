/**
 * Rows out of an Arrow IPC stream, as `service_kit.arrow_rows` writes them: scalar columns as
 * `utf8`, `float64` or `bool`, and the columns named in the schema metadata's `json_columns` as JSON
 * text, parsed back here. `rows` in the metadata is the row count.
 */
import { tableFromIPC } from 'apache-arrow';

export type Row = Record<string, unknown>;

/** The rows of one stream, every column by name. A JSON column whose text does not parse is kept as
 *  the text: the row is shown as it came rather than dropped. */
export function rowsOf(bytes: Uint8Array): Row[] {
	const table = tableFromIPC(bytes);
	const json = new Set<string>(JSON.parse(table.schema.metadata.get('json_columns') ?? '[]'));
	const names = table.schema.fields.map((field) => field.name);
	const rows: Row[] = [];
	for (let index = 0; index < table.numRows; index++) {
		const row: Row = {};
		for (const name of names) {
			const value: unknown = table.getChild(name)?.get(index) ?? null;
			if (json.has(name) && typeof value === 'string') {
				try {
					row[name] = JSON.parse(value);
				} catch {
					row[name] = value;
				}
			} else row[name] = value;
		}
		rows.push(row);
	}
	return rows;
}
