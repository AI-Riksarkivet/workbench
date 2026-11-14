import { redirect } from '@sveltejs/kit';
import { linkWith, projectOf } from '#lib/project.ts';
import type { PageLoad } from './$types';

// The datasets are the place: the landing is their list, with the reader's project kept.
export const load: PageLoad = ({ url }) => {
	redirect(307, linkWith(projectOf(url))('/datasets'));
};
