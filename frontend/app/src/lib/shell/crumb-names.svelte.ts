/**
 * The names a page gives the breadcrumb's parameter segments: a project's title for `/projects/<id>`, an item's name
 * for its canvas, in place of the id the URL carries. The id stays on the crumb as its tooltip.
 *
 * Keyed by the crumb's path, so a name is only ever shown for the exact project or task it was read for: a page names
 * its own paths while it is mounted (`nameCrumb` returns the forgetting), and a path nobody names reads as its id.
 */
import { SvelteMap } from 'svelte/reactivity';

export interface CrumbName {
	label: string;
	/** What the crumb's tooltip says: the id the name stands for. */
	title: string;
}

export const crumbNames = new SvelteMap<string, CrumbName>();

/** Name the crumb at `path` until the returned function is called. */
export function nameCrumb(path: string, name: CrumbName): () => void {
	crumbNames.set(path, name);
	return () => {
		if (crumbNames.get(path) === name) crumbNames.delete(path);
	};
}
