/**
 * The knowledge graph, read from the viewer's graph doors as the person (`/api/graph/*`): whether the
 * dataset's graph is built, a subgraph (the overview, or one entity's neighbourhood), one entity with its
 * clips and neighbours, and the entities whose names match a few letters.
 *
 * The Cypher console is not a read: it runs what the person wrote, and pressing Run again must run it
 * again, so it is a call (`runCypher`) and never cached. It changes nothing, so it is no write either,
 * and the activity log does not list it.
 */
import * as v from 'valibot';
import {
	GraphCypherResponseSchema,
	getGraphEntity,
	getGraphStatus,
	getGraphSubgraph,
	searchGraphEntities,
	type GraphCypherResponse,
} from '@workbench/explorer-api';
import { query } from '#lib/reads.svelte.ts';
import { readJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';

/** `dataset` keys each read: the client reads the active view's. */
export const graphStatus = query(v.string(), () => getGraphStatus());

export const graphSubgraph = query(
	v.object({ dataset: v.string(), entityId: v.nullable(v.string()), limit: v.number() }),
	({ entityId, limit }) => getGraphSubgraph(entityId ?? undefined, limit),
);

export const graphEntity = query(
	v.object({ dataset: v.string(), entityId: v.string() }),
	({ entityId }) => getGraphEntity(entityId),
);

export const graphMatches = query(v.object({ dataset: v.string(), q: v.string() }), ({ q }) =>
	searchGraphEntities(q),
);

/** The rows the viewer's console answers at most. */
const CYPHER_LIMIT = 200;

/** Run one Cypher query against `dataset`'s graph. A refusal or an unreachable viewer is the result's
 *  failure, never a throw. */
export function runCypher(
	dataset: string,
	cypher: string,
): Promise<ApiResult<GraphCypherResponse>> {
	return readJSON(
		serviceUrl('viewer', `/api/graph/cypher?${new URLSearchParams({ dataset })}`),
		GraphCypherResponseSchema,
		'the viewer',
		{
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ query: cypher, limit: CYPHER_LIMIT }),
		},
	);
}
