import { Database } from '@lucide/svelte';
import type { PageNav, PageNavLeaf } from '#lib/shell/nav-config.ts';

/**
 * The app's one navigation: every page, in one sidebar, on every page. One name per page: a page's
 * sidebar row, breadcrumb crumb, `h1` and `<title>` say the same name (`PAGE_NAMES`).
 */

/** Drop a single trailing slash (except on root `/`), so a match is trailing-slash-robust. */
const norm = (p: string): string => (p.length > 1 && p.endsWith('/') ? p.slice(0, -1) : p);

/** Active when the path is `href` or below it. */
const seg =
	(href: string) =>
	(p: string): boolean =>
		norm(p) === href || norm(p).startsWith(`${href}/`);

const DATASETS = { title: 'Datasets', path: '/datasets', icon: Database } as const;

/** The sidebar, its hrefs made by `link` (which resolves a path and carries the reader's project). */
export function appSidebar(link: (path: string) => string): PageNav {
	const datasets: PageNavLeaf = {
		title: DATASETS.title,
		href: link(DATASETS.path),
		match: seg(DATASETS.path),
		icon: DATASETS.icon,
	};
	return { groups: [{ items: [datasets] }] };
}

/** Every path that is a page of its own, with the name the sidebar gives it, for the breadcrumb. */
export const PAGE_NAMES: ReadonlyMap<string, string> = new Map([[DATASETS.path, DATASETS.title]]);

/** The `<title>` of a page named `name`. */
export const titleOf = (...names: string[]): string => [...names, 'workbench'].join(' · ');
