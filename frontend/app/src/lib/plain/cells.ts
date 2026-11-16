/**
 * A plain table's cell as text, by its column's kind: the viewer renders each cell from the column's
 * Arrow type (`viewer.services.plain_rows`), so a vector arrives as its dimension and first values, a
 * binary or blob cell as its size, a list or struct as JSON text. Pure, so the table and the row panel
 * write a cell the same way.
 */
import type { ColumnKind } from '#lib/api/datasets.api.ts';

const isRecord = (value: unknown): value is Record<string, unknown> =>
	typeof value === 'object' && value !== null && !Array.isArray(value);

/** A byte count a person reads. */
function bytes(size: number): string {
	if (size < 1024) return `${size} B`;
	if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
	return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
}

export function cellText(kind: ColumnKind, value: unknown): string {
	if (value === null || value === undefined) return '';
	if ((kind === 'binary' || kind === 'blob') && isRecord(value)) {
		return typeof value.size === 'number' ? bytes(value.size) : 'bytes';
	}
	if (kind === 'vector' && isRecord(value) && Array.isArray(value.head)) {
		const head = value.head.map((n) => (typeof n === 'number' ? n.toFixed(3) : String(n)));
		return `[${head.join(', ')}${Number(value.dim) > head.length ? ', …' : ''}] · ${String(value.dim)}d`;
	}
	if (kind === 'timestamp' && typeof value === 'string') return value.replace('T', ' ');
	if (typeof value === 'string') return value;
	if (typeof value === 'number' || typeof value === 'boolean') return String(value);
	return JSON.stringify(value);
}

/** Whether a kind is right-aligned in a table, as numbers are. */
export const numeric = (kind: ColumnKind): boolean =>
	kind === 'integer' || kind === 'float' || kind === 'decimal';
