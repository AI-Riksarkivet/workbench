/**
 * The views of one dataset beside its dock (`/datasets/[id]`): the atlas, the knowledge graph and the
 * topic tree, each a page of its own under the dataset.
 *
 * Every view is always listed. One the dataset cannot show is listed DISABLED with its reason, phrased
 * about the dataset ("declares no knowledge graph") rather than the product: an absent entry reads as a
 * feature the workbench lacks, a disabled one says what to declare. The gates read the names the viewer
 * itself reads: `topics` is the capability its topics door looks up, `graph` the one its graph door
 * looks up, and the atlas gates on the declared spaces, which the atlas door draws.
 */
import type { DatasetView } from '@workbench/explorer-api/descriptor';

/** A view's route, relative to the dataset: the route ids `/datasets/[id]/atlas` and so on. */
export type DatasetPage = 'atlas' | 'graph' | 'tree';

export interface DatasetPageEntry {
	page: DatasetPage;
	/** The page's one name: its tab, breadcrumb crumb, `h1` and `<title>`. */
	name: string;
	/** Why it cannot be opened for this dataset, or null when it can. */
	unavailable: string | null;
}

/** The pages' names, in the order they are listed. */
export const DATASET_PAGE_NAMES: Readonly<Record<DatasetPage, string>> = {
	atlas: 'Atlas',
	graph: 'Graph',
	tree: 'Tree',
};

const AREAS: readonly {
	page: DatasetPage;
	available: (view: DatasetView, rowTable: string | null) => boolean;
	why: string;
}[] = [
	{
		page: 'atlas',
		// An atlas space is bound to one table, so a space over `pages` has nothing to draw while the
		// reader searches `lines`. With no table chosen, any declared space opens it.
		available: (view, rowTable) =>
			rowTable === null
				? view.atlasSpaces.length > 0
				: view.atlasSpaces.some((space) => space.table === rowTable),
		why: 'this dataset declares no atlas projection for the table in view',
	},
	{
		page: 'graph',
		available: (view) => view.hasCapability('graph'),
		why: 'this dataset declares no knowledge graph',
	},
	{
		page: 'tree',
		// The schema, not whether the hierarchy has been computed: a declared tree that is not built yet
		// opens, and its page says none has been written.
		available: (view) => view.hasCapability('topics'),
		why: 'this dataset declares no topic hierarchy',
	},
];

/** The dataset's views, every one listed; `view` is null while its descriptor is read, and for a
 *  dataset shown as a plain table (`plain`), which declares none of them. */
export function datasetPages(
	view: DatasetView | null,
	rowTable: string | null = null,
	plain = false,
): DatasetPageEntry[] {
	return AREAS.map(({ page, available, why }) => ({
		page,
		name: DATASET_PAGE_NAMES[page],
		unavailable: plain
			? 'this dataset carries no workbench descriptor, so it is shown as a plain table'
			: view === null
				? 'the dataset is still being read'
				: available(view, rowTable)
					? null
					: why,
	}));
}
