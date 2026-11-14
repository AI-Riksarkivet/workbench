/**
 * Media for `<img>` and `<video>`, which send no token: the viewer's `POST /api/signed-media` resolves
 * each cell by the person's own read and answers a short signed link per item, valid for minutes, that
 * is the permission to read that one cell (owner decision on #9). The link is a path on the viewer's
 * own root, so it is joined to the viewer's public base like any other of its paths.
 */
import * as v from 'valibot';
import { query } from '#lib/reads.svelte.ts';
import { orFail } from './datasets.api.ts';
import { readJSON } from './client.ts';
import { serviceUrl } from './services.ts';

/** The most items one batch may name (the viewer's `MAX_BATCH`). */
export const MAX_BATCH = 200;

const MediaUrls = v.object({
	urls: v.array(
		v.object({
			url: v.nullish(v.pipe(v.string(), v.startsWith('/'))),
			expires_at: v.nullish(v.number()),
			detail: v.nullish(v.string()),
		}),
	),
});

const SignInput = v.object({
	dataset: v.string(),
	role: v.picklist(['thumbnail', 'media']),
	docIds: v.pipe(v.array(v.string()), v.minLength(1), v.maxLength(MAX_BATCH)),
});

/** One signed link per doc id, in the request's order: a same-origin URL, or why there is none. */
export type SignedLink = { url: string; expiresAt: number | null } | { url: null; detail: string };

export const signedLinks = query(SignInput, async ({ dataset, role, docIds }) => {
	const body = { dataset, items: docIds.map((doc_id) => ({ role, doc_id })) };
	const answer = orFail(
		await readJSON(serviceUrl('viewer', '/api/signed-media'), MediaUrls, 'the viewer', {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(body),
		}),
	);
	return new Map<string, SignedLink>(
		docIds.map((id, index) => {
			const item = answer.urls[index];
			const link: SignedLink = item?.url
				? { url: serviceUrl('viewer', item.url), expiresAt: item.expires_at ?? null }
				: { url: null, detail: item?.detail ?? 'the viewer named no link for this item' };
			return [id, link];
		}),
	);
});
