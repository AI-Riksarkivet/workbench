/**
 * The reader's column preferences for the Results panel's tables, kept in this browser.
 *
 * Two shapes:
 *   • prefs ({cols, known, widths, wrap}) for the results table: visible columns, dragged widths (px)
 *     and the wrap toggle in ONE record. `known` is the set of columns that existed when the reader last
 *     chose, so a column the app adds later starts visible when it is a default, instead of being hidden
 *     by a verbatim restore.
 *   • plain, a string array, for the browse table's fixed column set.
 *
 * Storage is best-effort: a browser that keeps no site storage (a private window, blocked site data)
 * falls back to the defaults and never throws.
 */
import * as v from 'valibot';

const SavedTablePrefsSchema = v.object({
	cols: v.array(v.string()),
	known: v.array(v.string()),
	widths: v.record(v.string(), v.number()),
	wrap: v.boolean(),
});

const StringArraySchema = v.array(v.string());

/** Per-table preferences: visible columns, dragged widths (px, by column key; absent is auto-sized),
 *  and whether long text wraps. */
export type TablePrefs = {
	cols: string[];
	widths: Record<string, number>;
	wrap: boolean;
};

export function loadTablePrefs(args: {
	storageKey: string;
	/** Every column key the app defines. */
	allKeys: string[];
	/** The columns a first visit shows, and which new columns start visible. */
	defaults: string[];
}): TablePrefs {
	const { storageKey, allKeys, defaults } = args;
	try {
		const raw = localStorage.getItem(storageKey);
		if (raw) {
			const saved = v.parse(SavedTablePrefsSchema, JSON.parse(raw));
			const fresh = allKeys.filter((k) => !saved.known.includes(k) && defaults.includes(k));
			return { cols: [...saved.cols, ...fresh], widths: saved.widths, wrap: saved.wrap };
		}
	} catch {
		/* fall through to defaults */
	}
	return { cols: [...defaults], widths: {}, wrap: false };
}

export function persistTablePrefs(storageKey: string, prefs: TablePrefs, allKeys: string[]): void {
	try {
		localStorage.setItem(
			storageKey,
			JSON.stringify({ cols: prefs.cols, known: allKeys, widths: prefs.widths, wrap: prefs.wrap }),
		);
	} catch {
		/* best-effort */
	}
}

export function loadCols(storageKey: string, defaults: string[]): string[] {
	try {
		const raw = localStorage.getItem(storageKey);
		if (raw) return v.parse(StringArraySchema, JSON.parse(raw));
	} catch {
		/* fall through to defaults */
	}
	return [...defaults];
}

export function persistCols(storageKey: string, cols: string[]): void {
	try {
		localStorage.setItem(storageKey, JSON.stringify(cols));
	} catch {
		/* best-effort */
	}
}
