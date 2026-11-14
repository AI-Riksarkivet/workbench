import { setActiveTable, setActiveView } from '@workbench/explorer-api/descriptor';
import { datasetView } from '#lib/api/explore.api.ts';
import { explore } from '#lib/explore/explore.svelte.ts';
import { voiceSearch } from '#lib/explore/voice-search.svelte.ts';
import type { PageLoad } from './$types';

/**
 * The dataset the page explores, made the explorer client's active view before any panel renders: every
 * renderer reads rows through it. The dock's panels read the dataset from `explore`, since a popped-out
 * panel lives outside this page's component tree; it is opened here on every navigation to another
 * dataset, with its voice tables' probe and the links that brought the reader (`?topic=`, `?doc=&t=`,
 * `?voice_doc=`). A descriptor the reader cannot read is the page's own "no such dataset" state, not an error
 * page: the sidebar and the way back stay.
 */
export const load: PageLoad = async ({ params, url, parent }) => {
	// SvelteKit runs a page's load beside its layout's: wait for the layout's sign-in and the transport
	// it installs, or the descriptor goes out with no credential and is refused.
	await parent();
	explore.open(params.id);
	setActiveTable(url.searchParams.get('table'));
	let view: Awaited<ReturnType<typeof datasetView>>;
	try {
		view = await datasetView(params.id);
	} catch (err) {
		return { id: params.id, failure: failureOf(err) };
	}
	// A navigation to another dataset started while this one's descriptor was read: that load owns the
	// client's active view now, and this one's answer must not overwrite it.
	if (explore.dataset !== params.id) return { id: params.id, failure: null };
	setActiveView(view);
	voiceSearch.probe();
	followDeepLink(url.searchParams);
	return { id: params.id, failure: null };
};

/** The links another page hands this one: a topic to browse, a moment of a document to play, or a voice
 *  to match (`voice_doc` with `voice_turn`, `voice_speaker` or `voice_t`). */
function followDeepLink(params: URLSearchParams): void {
	const topic = params.get('topic');
	if (topic) explore.apply({ ...explore.spec, q: '', topic });
	const voiceDoc = params.get('voice_doc');
	if (voiceDoc) {
		const turn = params.get('voice_turn');
		const speaker = params.get('voice_speaker');
		const at = params.get('voice_t');
		if (turn !== null && Number.isInteger(Number(turn))) {
			voiceSearch.request({ docId: voiceDoc, turnId: Number(turn) });
		} else if (speaker) {
			voiceSearch.request({ docId: voiceDoc, speaker });
		} else if (at !== null && Number.isFinite(Number(at))) {
			voiceSearch.request({ docId: voiceDoc, t: Number(at) });
		}
	}
	const doc = params.get('doc');
	if (doc) {
		const t = Number(params.get('t'));
		const start = Number.isFinite(t) ? Math.max(0, t) : 0;
		explore.active = explore.playerRow(doc, start, start);
	}
}

function failureOf(err: unknown): string {
	if (typeof err === 'object' && err !== null && 'detail' in err) {
		return String((err as { detail: unknown }).detail);
	}
	return err instanceof Error ? err.message : String(err);
}
