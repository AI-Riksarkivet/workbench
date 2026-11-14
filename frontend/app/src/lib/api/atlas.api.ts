/**
 * The atlas: the viewer's `GET /api/atlas/points`, one Arrow IPC stream of a projection's points
 * (float16 `x`/`y`, the identity keys, `rowid`, the `doc` and `cluster` codes), read as the person.
 *
 * The door answers a strong ETag of (dataset, space, table version) with `private, no-cache`, so the
 * browser's own HTTP cache keeps the bytes and revalidates them on every use (RFC 9111 § 4.3.1): the
 * request goes with `If-None-Match`, the viewer runs the person's own read again, and a 304 means the
 * version stands and no bytes travel. The cache is the browser's, kept across reloads and keyed by URL;
 * the read still reaches the viewer each time, so a person the catalog refuses gets the refusal, never
 * another person's cached points.
 */
import { tableFromIPC } from 'apache-arrow';
import * as v from 'valibot';
import { query } from '#lib/reads.svelte.ts';
import { orFail } from './datasets.api.ts';
import { readBytes } from './client.ts';
import { serviceUrl } from './services.ts';

export interface AtlasPoints {
	x: Float32Array;
	y: Float32Array;
	/** One cluster label per point, or null when the space has no cluster column. */
	cluster: (string | null)[];
	/** The ETag the points were answered with. */
	etag: string | null;
}

function decodePoints(bytes: Uint8Array, etag: string | null): AtlasPoints {
	const table = tableFromIPC(bytes);
	const xs = table.getChild('x');
	const ys = table.getChild('y');
	const clusters = table.getChild('cluster');
	const x = new Float32Array(table.numRows);
	const y = new Float32Array(table.numRows);
	const cluster: (string | null)[] = [];
	for (let i = 0; i < table.numRows; i++) {
		x[i] = Number(xs?.get(i) ?? Number.NaN);
		y[i] = Number(ys?.get(i) ?? Number.NaN);
		const label: unknown = clusters?.get(i);
		cluster.push(label === null || label === undefined ? null : String(label));
	}
	return { x, y, cluster, etag };
}

const AtlasInput = v.object({ dataset: v.string(), space: v.nullish(v.string()) });

export const atlasPoints = query(AtlasInput, async ({ dataset, space }): Promise<AtlasPoints> => {
	const params = new URLSearchParams({ dataset, ...(space ? { space } : {}) });
	const read = orFail(
		await readBytes(serviceUrl('viewer', `/api/atlas/points?${params}`), 'the viewer'),
	);
	return decodePoints(read.bytes, read.etag);
});
