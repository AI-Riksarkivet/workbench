/**
 * The atlas, read from the viewer's atlas doors as the person.
 *
 * Points: `GET /api/atlas/points`, one Arrow IPC stream of a projection's points (float16 `x`/`y`, the
 * identity keys, `rowid`, the `doc` dictionary, the `cluster` codes and the declared channels). The door
 * answers a strong ETag of (dataset, space, table version) with `private, no-cache`, so the browser's own
 * HTTP cache keeps the bytes and revalidates them on every use (RFC 9111 § 4.3.1): the request goes with
 * `If-None-Match`, the viewer runs the person's own read again, and a 304 means the version stands and no
 * bytes travel. The cache is the browser's, kept across reloads and keyed by URL; the read still reaches
 * the viewer each time, so a person the catalog refuses gets the refusal, never another person's points.
 *
 * Status: `GET /api/atlas/status`, which declared spaces are built.
 *
 * Rows: a selection's or a point's rows by their stable `_rowid` (`POST /api/atlas/chunks`), which every
 * dataset's points carry whatever its identity key, so a point opens on any dataset. A selection is a
 * gesture, not a read anyone holds again, so it is a call on the same transport, not a cached read.
 */
import * as v from 'valibot';
import {
	decodeAtlasPoints,
	getAtlasChunks,
	getAtlasStatus,
	type AtlasPoints,
	type Hit,
} from '@workbench/explorer-api';
import { query } from '#lib/reads.svelte.ts';
import { orFail } from './datasets.api.ts';
import { datasetView } from './explore.api.ts';
import { readBytes } from './client.ts';
import { serviceUrl } from './services.ts';

const AtlasInput = v.object({ dataset: v.string(), space: v.string() });

/** One space's points, decoded with `dataset`'s own view (its identity keys and channels), never with
 *  whichever dataset is active when the answer lands. */
export const atlasPoints = query(AtlasInput, async ({ dataset, space }): Promise<AtlasPoints> => {
	const params = new URLSearchParams({ dataset, space });
	const [view, read] = await Promise.all([
		datasetView(dataset),
		readBytes(serviceUrl('viewer', `/api/atlas/points?${params}`), 'the viewer'),
	]);
	return decodeAtlasPoints(orFail(read).bytes, view, space);
});

/** Which declared spaces are built, read for `space`'s row count too. */
export const atlasStatus = query(AtlasInput, ({ space }) => getAtlasStatus(space));

/** The rows of the points whose stable row ids are `rowids` (the viewer answers at most 1000). */
export function atlasRows(rowids: number[]): Promise<Hit[]> {
	return getAtlasChunks(rowids);
}
