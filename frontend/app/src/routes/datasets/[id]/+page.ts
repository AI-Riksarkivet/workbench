import { followLink } from '#lib/dock/follow-link.ts';
import { readDockLink } from '#lib/dock/link.ts';
import { plain } from '#lib/plain/plain.svelte.ts';
import type { PageLoad } from './$types';

/**
 * The dock opens on what its link says (`#lib/dock/follow-link.ts`), once the layout made the dataset the active
 * view. The link is the page's data, so a link to the same dataset, which runs this load again without
 * remounting the page, brings its panels in front too. A plain table's link also carries its set-ups, its
 * search and the node its rows were filtered to, which the plain table takes here (`plain.restore`).
 */
export const load: PageLoad = async ({ url, parent }) => {
	const { view, facts } = await parent();
	if (view !== null) return { link: followLink(view, url.searchParams) };
	const link = readDockLink(url.searchParams);
	if (facts !== null && !facts.stamped && plain.facts?.id === facts.id) {
		if (Object.keys(link.setups).length > 0 || link.node !== null)
			plain.restore(link.setups, link.node);
		if (link.q) plain.search(link.q);
	}
	return { link };
};
