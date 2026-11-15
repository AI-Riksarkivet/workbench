import { redirect } from '@sveltejs/kit';
import { resolve } from '$app/paths';
import type { PageLoad } from './$types';

// A project's items are listed on its own page (its queue), so the path above an item's labeling page, which the
// breadcrumb links, goes there rather than answering 404.
export const load: PageLoad = ({ params, url }) => {
	const project = url.searchParams.get('project');
	const query = project === null ? '' : `?${new URLSearchParams({ project })}`;
	redirect(307, `${resolve('/projects/[project]', { project: params.project })}${query}`);
};
