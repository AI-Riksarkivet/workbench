/**
 * Annotation history — who saved a task's work, when, and what it held. Framework-agnostic (no Svelte): reads a
 * unit's history of saves (the annotator's bounded history of the task's draft) and a past save's rows, and diffs a
 * past save against the current rows by id. The compare-versions panel is a thin view over this. See the annotator's
 * `/versions` door and its `?version` read.
 */
import { type Table, tableFromIPC } from 'apache-arrow';

export interface AnnotationVersion {
	version: number;
	timestamp: string;
	/** Who saved it. */
	author: string;
	count: number;
}

/** Parsed at the wire (#94), not cast: a drifted /versions answer must fail with a named error,
 *  not render an empty-looking history over a unit that has one. Hand-rolled rather than valibot
 *  because this package deliberately carries no valibot dependency (arrow + explorer-api only). */
export function parseVersions(raw: unknown): AnnotationVersion[] {
	if (!Array.isArray(raw)) throw new Error('versions answered a non-array body');
	return raw.map((entry: unknown, i) => {
		if (entry === null || typeof entry !== 'object') {
			throw new Error(`versions[${i}] is not an object`);
		}
		const e = entry as {
			version?: unknown;
			timestamp?: unknown;
			author?: unknown;
			count?: unknown;
		};
		if (
			typeof e.version !== 'number' ||
			typeof e.timestamp !== 'string' ||
			typeof e.author !== 'string' ||
			typeof e.count !== 'number'
		) {
			throw new Error(`versions[${i}] has a drifted shape`);
		}
		return { version: e.version, timestamp: e.timestamp, author: e.author, count: e.count };
	});
}

/** The review-visible fields a diff compares (geometry moves are not "review" changes). */
export interface RowSig {
	label: string;
	status: string;
	shape: string;
	group: string;
	text: string;
}

/** A stable signature of a row's reviewable content — equal signatures ⇒ no change.
 *  JSON-encode the field array so it's provably injective: no separator to collide
 *  ("ab","" vs "a","b" encode differently) and any value (quotes, control chars) is safe. */
export function rowSignature(r: RowSig): string {
	return JSON.stringify([
		r.label ?? '',
		r.status ?? '',
		r.shape ?? '',
		r.group ?? '',
		r.text ?? '',
	]);
}

/** Insert `suffix` into the annotations path (before any query) and merge `extra` params —
 *  so `/api/annotations/a/b/c?dataset=X` → `/api/annotations/a/b/c/versions?dataset=X&limit=20`. */
function buildUrl(annotationsUrl: string, suffix: string, extra: Record<string, string>): string {
	const [path, search = ''] = annotationsUrl.split('?');
	const params = new URLSearchParams(search);
	for (const [k, val] of Object.entries(extra)) params.set(k, val);
	const qs = params.toString();
	return `${path}${suffix}${qs ? `?${qs}` : ''}`;
}

/** The unit's edit history (most-recent first, capped). */
export async function fetchVersions(
	annotationsUrl: string,
	limit = 20,
): Promise<AnnotationVersion[]> {
	const res = await fetch(buildUrl(annotationsUrl, '/versions', { limit: String(limit) }));
	if (!res.ok) throw new Error(`versions failed (HTTP ${res.status})`);
	return parseVersions(await res.json());
}

/** A historical version's rows as id→signature (for diffing against the current rows). */
export async function fetchVersionSignatures(
	annotationsUrl: string,
	version: number,
): Promise<Map<string, string>> {
	const res = await fetch(buildUrl(annotationsUrl, '', { version: String(version) }));
	if (!res.ok) throw new Error(`version read failed (HTTP ${res.status})`);
	return signaturesOf(tableFromIPC(new Uint8Array(await res.arrayBuffer())));
}

/** A version's rows as id→signature, the shape {@link diffSignatures} compares. */
export function signaturesOf(table: Table): Map<string, string> {
	const sigs = new Map<string, string>();
	for (const row of table.toArray()) {
		const r = row as Record<string, unknown>;
		sigs.set(
			String(r.id),
			rowSignature({
				label: String(r.label ?? ''),
				status: String(r.status ?? ''),
				shape: String(r.shape_type ?? ''),
				group: String(r.group ?? ''),
				text: String(r.text ?? ''),
			}),
		);
	}
	return sigs;
}

export interface RowDiff {
	added: string[]; // present now, absent in the past version
	removed: string[]; // present then, absent now
	changed: string[]; // present in both, reviewable content differs
}

/** Diff a PAST version (id→sig) against the CURRENT rows (id→sig). */
export function diffSignatures(past: Map<string, string>, current: Map<string, string>): RowDiff {
	const added: string[] = [];
	const removed: string[] = [];
	const changed: string[] = [];
	for (const [id, sig] of current) {
		if (!past.has(id)) added.push(id);
		else if (past.get(id) !== sig) changed.push(id);
	}
	for (const id of past.keys()) if (!current.has(id)) removed.push(id);
	return { added, removed, changed };
}
