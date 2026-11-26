/**
 * Saved views shared by their link (ADR 0022): the annotator's `/views` doors, same-origin on the DPoP seam
 * (`client.ts`). Sharing sends a view's name and snapshot and answers its id, which the app makes a link of; opening the
 * link is a read of the view's dataset under the reader's own rights, refused (403 or 404, with the door's `detail`)
 * exactly as any read of that dataset is, and a 404 once its owner stopped sharing it. The listing answers only the
 * caller's own shared views of one dataset.
 *
 * A snapshot is answered as the app sent it and is never trusted for it: it is checked as a view read from this
 * browser's storage is, part by part, when it is applied (`#lib/dock/views.svelte.ts`).
 */
import * as v from 'valibot';
import type { SharedViewsDoor } from '#lib/dock/views.svelte.ts';
import { command } from '#lib/reads.svelte.ts';
import { readJSON, writeJSON, writeNoContent, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';

const UPSTREAM = 'the annotator';

const SharedViewSchema = v.object({
	view_id: v.string(),
	owner: v.string(),
	dataset: v.string(),
	name: v.string(),
	snapshot: v.record(v.string(), v.unknown()),
	shared_at: v.nullable(v.string()),
});
export type SharedView = v.InferOutput<typeof SharedViewSchema>;

const SharedViewsSchema = v.object({
	views: v.array(SharedViewSchema),
	/** Whether the caller shares more views of the dataset than the listing answers. */
	more: v.boolean(),
});
export type SharedViewsListing = v.InferOutput<typeof SharedViewsSchema>;

const enc = encodeURIComponent;

/** The shared view `viewId`, or the door's refusal. */
export function openSharedView(viewId: string): Promise<ApiResult<SharedView>> {
	return readJSON(serviceUrl('annotator', `/views/${enc(viewId)}`), SharedViewSchema, UPSTREAM);
}

/** The views the caller shares of `dataset`, newest first, and whether there are more than it answers. */
function listSharedViews(dataset: string): Promise<ApiResult<SharedViewsListing>> {
	return readJSON(
		serviceUrl('annotator', `/views?${new URLSearchParams({ dataset })}`),
		SharedViewsSchema,
		UPSTREAM,
	);
}

/** Share a saved view of `dataset`: its name and what it holds. */
const shareView = command(
	(input: { name: string }) => `Share view ${input.name}`,
	'unchecked',
	({
		dataset,
		name,
		snapshot,
	}: {
		dataset: string;
		name: string;
		snapshot: Record<string, unknown>;
	}): Promise<ApiResult<SharedView>> =>
		writeJSON(
			serviceUrl('annotator', '/views'),
			{ dataset, name, snapshot },
			SharedViewSchema,
			UPSTREAM,
		),
);

/** Stop sharing a view the caller shares: its link opens nothing after. */
const stopSharingView = command(
	(input: { name: string }) => `Stop sharing view ${input.name}`,
	'unchecked',
	({ viewId }: { viewId: string; name: string }): Promise<ApiResult<null>> =>
		writeNoContent(serviceUrl('annotator', `/views/${enc(viewId)}`), UPSTREAM),
);

/** One dataset's shared views, as the views sidebar's state model calls them (`DockViews`). */
export function sharedViewsOf(dataset: string): SharedViewsDoor {
	return {
		list: () => listSharedViews(dataset),
		share: (name, snapshot) =>
			shareView({
				dataset,
				name,
				snapshot: {
					layout: snapshot.layout,
					table: snapshot.table,
					setups: snapshot.setups,
					q: snapshot.q,
					node: snapshot.node,
				},
			}),
		stop: (viewId, name) => stopSharingView({ viewId, name }),
	};
}
