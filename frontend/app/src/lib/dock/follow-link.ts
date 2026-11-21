/**
 * Open a dataset's dock on what its link says (`#lib/dock/link.ts`), once the dataset is the active view: the
 * search and the topic, the row opened and the graph's entity. The panels the link names are the page's to bring in front, over
 * the layout it restores: the link is answered for that.
 */
import type { SearchMode } from '@workbench/explorer-api';
import { explore } from '#lib/explore/explore.svelte.ts';
import { graphExplorer } from '#lib/graph/graph-explorer.svelte.ts';
import { readDockLink, type DockLink } from './link.ts';

export function followLink(
	view: { id: string; hasMode(mode: SearchMode): boolean },
	params: Pick<URLSearchParams, 'get' | 'getAll' | 'has'>,
): DockLink {
	const link = readDockLink(params);
	graphExplorer.open(view.id, link.entity);
	if (link.q || link.topic) {
		const mode =
			link.mode !== null && view.hasMode(link.mode as SearchMode) ? link.mode : undefined;
		explore.apply({
			...explore.spec,
			q: link.q ?? '',
			topic: link.topic ?? undefined,
			...(mode ? { mode: mode as SearchMode } : {}),
		});
	}
	if (link.row !== null) explore.openLinked(link.row, link.t);
	else if (link.doc !== null)
		explore.active = explore.playerRow(link.doc, link.t ?? 0, link.t ?? 0);
	// A stamped dataset draws its Atlas, Graph and Tree from its descriptor: a set-up the link carries
	// is for a table with none, so it is not used, and the page says so rather than dropping it unseen.
	const carried = Object.keys(link.setups).length > 0 || link.node !== null || link.setupProblem;
	return carried
		? {
				...link,
				setups: {},
				node: null,
				setupProblem:
					'set-ups of the Atlas, Graph and Tree apply to a table with no workbench descriptor, and this dataset has one, which draws them',
			}
		: link;
}
