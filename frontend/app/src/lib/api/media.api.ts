/**
 * Media for `<img>`, `<video>` and `<audio>`, which send no token: the viewer's `POST /api/signed-media`
 * resolves each cell by the person's own read and answers a short signed link per item, valid for
 * minutes, that is the permission to read that one cell (owner decision on #9). The link is a path on the
 * viewer's own root, so it is joined to the viewer's public base like any other of its paths. Asks are
 * batched by `MediaLinks`; this module is the one batch door it calls.
 */
import * as v from 'valibot';
import { readJSONDated } from './client.ts';
import { lifetimeOf, MediaLinks, type MediaItem, type MediaLink } from './media-links.svelte.ts';
import { serviceUrl } from './services.ts';

const MediaUrls = v.object({
	urls: v.array(
		v.object({
			url: v.nullish(v.pipe(v.string(), v.startsWith('/'))),
			expires_at: v.nullish(v.number()),
			detail: v.nullish(v.string()),
		}),
	),
});

async function signBatch(dataset: string, items: MediaItem[]): Promise<MediaLink[]> {
	const answer = await readJSONDated(
		serviceUrl('viewer', '/api/signed-media'),
		MediaUrls,
		'the viewer',
		{
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ dataset, items }),
		},
	);
	if (!answer.ok) throw new Error(answer.detail);
	return items.map((_, index): MediaLink => {
		const item = answer.data.body.urls[index];
		return item?.url
			? {
					url: serviceUrl('viewer', item.url),
					lifetime: lifetimeOf(item.expires_at ?? null, answer.data.date),
				}
			: { url: null, detail: item?.detail ?? 'the viewer named no link for this item' };
	});
}

/** The page's one loader: every media element on every panel asks it. */
export const mediaLinks = new MediaLinks(signBatch);
