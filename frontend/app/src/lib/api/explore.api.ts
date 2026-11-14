/**
 * The explorer's reads, declared on the app's data layer (`reads.svelte.ts`): the dataset's view (its
 * descriptor, wrapped for the renderers), a search, a page of documents, a voice search by anchor, a
 * document's chunks and speaker turns, the filterable columns, the voice tables' status and the
 * viewer's health.
 * Each goes through `@workbench/explorer-api` on the app's transport (`explorer.ts`), so the call is the
 * one DPoP seam's, with a read's timeout.
 *
 * A search or voice search that carries a FILE (an image to search by, a clip to match a voice to) is
 * not one of these: a file is no argument a read can be keyed by, so `explore.svelte.ts` calls the
 * client for it directly, on the same transport.
 */
import * as v from 'valibot';
import {
	getDatasetView,
	getDiarization,
	getDocChunks,
	getHealth,
	getVoiceStatus,
	listColumns,
	listDocuments,
	search,
	voiceSimilar,
	type SearchSpec,
	type VoiceAnchor,
} from '@workbench/explorer-api';
import { query } from '#lib/reads.svelte.ts';

/** The dataset's descriptor as the renderers read it; a dataset the reader may not see is the viewer's
 *  refusal, as the read's error. */
export const datasetView = query(v.string(), (id) => getDatasetView(id, false));

/** A search with no file attached. `dataset` keys the read: the client reads the active view's. */
export const searchHits = query(
	'unchecked',
	({ spec }: { dataset: string; spec: Omit<SearchSpec, 'image'> }) => search(spec),
);

/** One page of the dataset's documents, the browse list shown before any search. */
export const documentsPage = query(
	v.object({ dataset: v.string(), page: v.number(), perPage: v.number() }),
	({ page, perPage }) => listDocuments(page, perPage),
);

/** The voices most like an anchor's (a speaker turn, a speaker, or a moment of a document). */
export const voiceHits = query(
	'unchecked',
	({
		anchor,
		n,
		excludeSameDoc,
	}: {
		dataset: string;
		anchor: VoiceAnchor;
		n: number;
		excludeSameDoc: boolean;
	}) => voiceSimilar(anchor, { n, excludeSameDoc }),
);

/** One document's chunks, in order: the player's transcript and timeline. */
export const documentChunks = query(
	v.object({ dataset: v.string(), docId: v.string() }),
	({ docId }) => getDocChunks(docId),
);

/** One document's speaker turns, where the dataset declares diarization. */
export const speakerTurns = query(
	v.object({ dataset: v.string(), docId: v.string() }),
	({ docId }) => getDiarization(docId),
);

/** The columns a filter can name, per dataset. */
export const filterColumns = query(v.string(), () => listColumns());

/** Whether the dataset's voice tables are built, which every voice affordance waits for. */
export const voiceStatus = query(v.string(), () => getVoiceStatus());

/** Whether the viewer reaches its encoders: the search modes that need one wait for it. */
export const serviceHealthRead = query(() => getHealth());
