import { bench } from '#lib/dock/bench.svelte.ts';
import type { PageLoad } from './$types';

// The dock's panels read the dataset from `bench`, since a popped-out panel lives outside this page's
// component tree; it is set here, before the page renders, on every navigation to another dataset.
export const load: PageLoad = ({ params }) => {
	bench.open(params.id);
	return { id: params.id };
};
