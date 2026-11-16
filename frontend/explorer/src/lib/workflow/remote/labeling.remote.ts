import { command } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@workbench/api/client';
import { parsed } from '@workbench/api/upstream';
import { annotatorJSON, sessionGate } from '$lib/server/doors';
import type { SaveResult } from '@workbench/labeling/tag-writer';

// The write this zone makes into the ANNOTATOR service, in the zone's remote-function dialect (the
// transport ruling, area 3), gated on a session (`sessionGate`).
//
// Media's workflow promotes its chunk tags to real annotation rows. Everything else on
// `/api/annotations/**` and `/api/assist/**` belongs to the annotator zone and is not reachable from
// here — which is what deleting the routes rather than widening them keeps true.
//
// Transport and gate live in `$lib/server/doors` over `@workbench/api/upstream` (#93). The answers are
// PARSED (#94): the schemas are typed against the wire contracts imported from `@workbench/labeling`, so
// a drift between this schema and the shared contract is a compile error, and a drifted wire answer
// is a named 502 the caller renders — never a silently-wrong count.

const SaveResultSchema: v.GenericSchema<unknown, SaveResult> = v.looseObject({
	saved: v.number(),
	version: v.number(),
});

/** One media-plane POST, parsed on the way out. */
const annotatorPost = <T>(
	path: string,
	body: unknown,
	schema: v.GenericSchema<unknown, T>,
): Promise<ApiResult<T>> =>
	annotatorJSON(path, { method: 'POST', body: JSON.stringify(body) }).then((r) =>
		parsed(r, schema, 'annotator'),
	);

/** One tagged unit: the doc key plus the NON-doc identity fields, positional (pairs with the
 *  descriptor's `keyFields` minus the doc key) — the arity-generic shape `@workbench/labeling/tag-writer`
 *  builds and this zone has always sent. */
const TagWriteSchema = v.object({
	doc_id: v.string(),
	keys: v.array(v.number()),
	labels: v.array(v.string()),
});

/** Persist the run's chunk tags as annotation ROWS — one merge_insert version server-side, idempotent
 *  by deterministic id. Adds and removes travel together so an un-tag is one version too. */
export const saveTagsAsAnnotations = command(
	v.object({
		adds: v.array(TagWriteSchema),
		removes: v.optional(v.array(TagWriteSchema)),
		base_version: v.optional(v.nullable(v.number())),
		/** The active dataset, or null for the default DB — read from the DatasetView at the call site
		 *  (a browser-side store the server cannot see). */
		dataset: v.nullable(v.string()),
	}),
	async ({ adds, removes, base_version, dataset }): Promise<ApiResult<SaveResult>> => {
		const refused = sessionGate();
		if (refused) return refused;
		const suffix = dataset ? `?dataset=${encodeURIComponent(dataset)}` : '';
		return annotatorPost(
			`/api/annotations/tags${suffix}`,
			{
				adds,
				removes,
				base_version,
			},
			SaveResultSchema,
		);
	},
);
