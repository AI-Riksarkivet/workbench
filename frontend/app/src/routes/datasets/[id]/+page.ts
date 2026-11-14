import { explore } from '#lib/explore/explore.svelte.ts';
import { voiceSearch } from '#lib/explore/voice-search.svelte.ts';
import type { PageLoad } from './$types';

/**
 * The dock follows the links that brought the reader (`?topic=`, `?doc=&t=`, `?voice_doc=`), once the
 * layout made the dataset the active view.
 */
export const load: PageLoad = async ({ url, parent }) => {
	const { view } = await parent();
	if (view !== null) followDeepLink(url.searchParams);
	return {};
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
