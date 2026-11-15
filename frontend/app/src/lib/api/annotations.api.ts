/**
 * One unit's annotations, as the labeling canvas reads and writes them: the annotator's
 * `/api/annotations/<key>` door, same-origin on the DPoP seam (`client.ts`).
 *
 * WHICH PROJECT. Annotations are a project's: they live in the project's own table, and the door reads each unit
 * against the task whose item it is. So every URL here carries `?project=`, `?task=` and `?dataset=`, built once by
 * {@link annotationsUrl}; the save, the version history and a historical read all extend that one URL, so none of them
 * can name another project, task or unit.
 *
 * The read is Arrow IPC with the Lance version in `X-Annotations-Version`, which a save echoes as `base_version`: a
 * table that moved under the reader answers 409, and the canvas reloads. The save takes no `Idempotency-Key`; the
 * version is what makes a resend safe, since a resend of a save that landed meets a moved version and is refused.
 */
import { type Table, tableFromIPC } from 'apache-arrow';
import * as v from 'valibot';
import { parseVersions, signaturesOf, type AnnotationVersion } from '@workbench/labeling/history';
import type { SavePayload } from '@workbench/labeling/annotations-client';
import { readBytes, readJSON, writeJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';

const UPSTREAM = 'the annotator';

/** The annotations door of one unit (`doc/…` key path) of a task's item: its project, its task and its dataset. */
export function annotationsUrl(unit: {
	project: string;
	task: string;
	dataset: string;
	key: string;
}): string {
	const path = unit.key.split('/').map(encodeURIComponent).join('/');
	const query = new URLSearchParams({
		project: unit.project,
		task: unit.task,
		dataset: unit.dataset,
	});
	return serviceUrl('annotator', `/api/annotations/${path}?${query}`);
}

/** `url` with `suffix` after its path and `extra` added to its query. */
function extend(url: string, suffix: string, extra: Record<string, string>): string {
	const [path, search = ''] = url.split('?');
	const params = new URLSearchParams(search);
	for (const [k, value] of Object.entries(extra)) params.set(k, value);
	return `${path}${suffix}?${params}`;
}

function decode(bytes: Uint8Array): ApiResult<Table> {
	try {
		return { ok: true, data: tableFromIPC(bytes) };
	} catch (err) {
		return {
			ok: false,
			status: 502,
			detail: `${UPSTREAM} answered a body that is not Arrow IPC: ${String(err)}`,
		};
	}
}

/** The unit's annotations and the version they were read at. */
export async function loadAnnotations(
	url: string,
): Promise<ApiResult<{ table: Table; version: number }>> {
	const read = await readBytes(url, UPSTREAM);
	if (!read.ok) return read;
	const table = decode(read.data.bytes);
	if (!table.ok) return table;
	return {
		ok: true,
		data: {
			table: table.data,
			version: Number(read.data.headers.get('x-annotations-version') ?? '0'),
		},
	};
}

/** Save one review delta as one version. `conflict` is the table having moved since the read, and `signed-out` a
 *  session that ended (the edits are the caller's to keep); any other refusal, and a save that got no answer (NOT
 *  CONFIRMED), throws with the door's words. */
export async function saveAnnotations(
	url: string,
	payload: SavePayload,
): Promise<'ok' | 'conflict' | 'signed-out'> {
	const result = await writeJSON(url, payload, v.unknown(), UPSTREAM);
	if (result.ok) return 'ok';
	if (result.status === 409) return 'conflict';
	if (result.status === 401) return 'signed-out';
	throw new Error(result.detail);
}

/** The unit's edit history, most recent first. */
export async function fetchVersions(url: string, limit = 20): Promise<AnnotationVersion[]> {
	const read = await readJSON(
		extend(url, '/versions', { limit: String(limit) }),
		v.unknown(),
		UPSTREAM,
	);
	if (!read.ok) throw new Error(read.detail);
	return parseVersions(read.data);
}

/** A historical version's rows as id → signature, to diff against the rows on the canvas. */
export async function fetchVersionSignatures(
	url: string,
	version: number,
): Promise<Map<string, string>> {
	const read = await readBytes(extend(url, '', { version: String(version) }), UPSTREAM);
	if (!read.ok) throw new Error(read.detail);
	const table = decode(read.data.bytes);
	if (!table.ok) throw new Error(table.detail);
	return signaturesOf(table.data);
}
