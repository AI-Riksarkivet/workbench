import { resolve } from '$app/paths';

/**
 * Where a link to one of a dataset's views as a page of its own (`/datasets/<id>/atlas`, `graph`, `tree`) lands:
 * the dataset's dock with that panel brought in front (`panel=`, `#lib/dock/link.ts`), keeping everything else
 * the link said (the project, the table, a topic, its fragment). The views are panels of the dock; these routes
 * are only where saved links to them still land.
 */
export function panelHref(
	id: string,
	panel: 'atlas' | 'graph' | 'tree',
	url: { search: string; hash: string },
): string {
	const query = new URLSearchParams(url.search);
	query.set('panel', panel);
	return `${resolve('/datasets/[id]', { id: encodeURIComponent(id) })}?${query}${url.hash}`;
}
