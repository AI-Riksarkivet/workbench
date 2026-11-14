import { resolve } from '$app/paths';

/**
 * The project a reader works in is part of the page's URL (`?project=<id>`), never a cookie or
 * storage: a reload and a shared link show the same project, and Back is the project as it was. Every
 * in-app link carries it (`linkWith`); a call to a door that is scoped to a project names it itself.
 */

/** The project the URL names, or null. */
export function projectOf(url: {
	searchParams: { get(name: string): string | null };
}): string | null {
	const project = url.searchParams.get('project');
	return project === null || project === '' ? null : project;
}

/** A function making an app path's href, resolved by `$app/paths` and carrying `project` when there
 *  is one. A path's own query is kept, with the project after it. */
export function linkWith(project: string | null): (path: string) => string {
	return (path) => {
		const url = new URL(path, 'http://app.invalid');
		if (project !== null) url.searchParams.set('project', project);
		// kit 3: a path without the leading slash is resolved as a pathname (query included), never
		// read as a route id. Its types admit only literal pathnames, and this one is built at run time.
		const resolvePathname = resolve as (pathname: string) => string;
		return resolvePathname(`${url.pathname.slice(1)}${url.search}`);
	};
}
