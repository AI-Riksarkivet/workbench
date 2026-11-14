/**
 * The search and viewer services' row wire, for the mock that stands in for them: rows as one Arrow IPC
 * stream, a column per field, numbers `Float64`, strings `Utf8`, booleans `Bool`, and anything nested or
 * mixed as its JSON text named under the schema metadata's `json_columns` (`service_kit.arrow_rows`).
 */
import {
	Bool,
	Float64,
	Schema,
	Table,
	tableToIPC,
	Utf8,
	vectorFromArray,
	type Vector,
} from 'apache-arrow';

type Kind = 'string' | 'number' | 'boolean' | 'json' | 'empty';

function kindOf(value: unknown): Kind {
	if (value === null || value === undefined) return 'empty';
	const t = typeof value;
	if (t === 'string' || t === 'number' || t === 'boolean') return t;
	return 'json';
}

function columnKind(values: readonly unknown[]): Kind {
	let kind: Kind = 'empty';
	for (const value of values) {
		const next = kindOf(value);
		if (next === 'empty') continue;
		if (next === 'json') return 'json';
		if (kind === 'empty') kind = next;
		else if (kind !== next) return 'json';
	}
	return kind;
}

export function rowsToArrowIPC(rows: readonly Record<string, unknown>[]): Uint8Array {
	const names = [...new Set(rows.flatMap((row) => Object.keys(row)))];
	const columns: Record<string, Vector> = {};
	const jsonColumns: string[] = [];
	for (const name of names) {
		const values = rows.map((row) => row[name] ?? null);
		const kind = columnKind(values);
		if (kind === 'number')
			columns[name] = vectorFromArray(values as (number | null)[], new Float64());
		else if (kind === 'boolean')
			columns[name] = vectorFromArray(values as (boolean | null)[], new Bool());
		else if (kind === 'json') {
			jsonColumns.push(name);
			columns[name] = vectorFromArray(
				values.map((value) => (value === null ? null : JSON.stringify(value))),
				new Utf8(),
			);
		} else columns[name] = vectorFromArray(values as (string | null)[], new Utf8());
	}
	const metadata = new Map<string, string>([
		['rows', String(rows.length)],
		['json_columns', JSON.stringify(jsonColumns)],
	]);
	const table = names.length === 0 ? new Table(new Schema([], metadata)) : new Table(columns);
	if (names.length > 0) for (const [key, value] of metadata) table.schema.metadata.set(key, value);
	return tableToIPC(table, 'stream');
}
