import { followLink } from '#lib/dock/follow-link.ts';
import { readDockLink } from '#lib/dock/link.ts';
import type { PageLoad } from './$types';

/**
 * The dock opens on what its link says (`#lib/dock/follow-link.ts`), once the layout made the dataset the active
 * view. The link is the page's data, so a link to the same dataset, which runs this load again without
 * remounting the page, brings its panels in front too.
 */
export const load: PageLoad = async ({ url, parent }) => {
	const { view } = await parent();
	const link = view !== null ? followLink(view, url.searchParams) : readDockLink(url.searchParams);
	return { link };
};
