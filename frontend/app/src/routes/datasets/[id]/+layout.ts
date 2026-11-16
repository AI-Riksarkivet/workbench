import { ApiError } from '@workbench/explorer-api';
import { setActiveTable, setActiveView } from '@workbench/explorer-api/descriptor';
import { datasetView } from '#lib/api/explore.api.ts';
import { explore } from '#lib/explore/explore.svelte.ts';
import { voiceSearch } from '#lib/explore/voice-search.svelte.ts';
import { failureText } from '#lib/failure.ts';
import type { LayoutLoad } from './$types';

/**
 * The dataset every page under `/datasets/[id]` reads, made the explorer client's active view before any
 * of them renders: every renderer reads rows through it. Panels and maps read the dataset from
 * `explore`, since a popped-out panel lives outside the page's component tree; it is opened here on
 * every navigation to another dataset, with its voice tables' probe. A descriptor the reader cannot read
 * is the layout's own "no such dataset" state, not an error page: the sidebar and the way back stay. A viewer
 * that could not be reached (no answer, or a gateway's 502, 503 or 504) is told apart from that, since the
 * dataset may well exist: `unreachable`, with a way to try again.
 */

/** Whether a failed read means the viewer was not reached, rather than that it answered no. */
function unreachable(err: unknown): boolean {
	if (err instanceof ApiError) return [502, 503, 504].includes(err.status);
	return err instanceof TypeError;
}

export const load: LayoutLoad = async ({ params, url, parent }) => {
	// SvelteKit runs a layout's load beside its parent's: wait for the root layout's sign-in and the
	// transport it installs, or the descriptor goes out with no credential and is refused.
	await parent();
	explore.open(params.id);
	setActiveTable(url.searchParams.get('table'));
	let view: Awaited<ReturnType<typeof datasetView>>;
	try {
		view = await datasetView(params.id);
	} catch (err) {
		return { id: params.id, failure: failureText(err), unreachable: unreachable(err), view: null };
	}
	// A navigation to another dataset started while this one's descriptor was read: that load owns the
	// client's active view now, and this one's answer must not overwrite it.
	if (explore.dataset !== params.id)
		return { id: params.id, failure: null, unreachable: false, view: null };
	setActiveView(view);
	voiceSearch.probe();
	return { id: params.id, failure: null, unreachable: false, view };
};
