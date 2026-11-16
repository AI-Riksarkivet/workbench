/**
 * Open a dataset's dock on what its link says (`#lib/dock/link.ts`), once the dataset is the active view: the
 * search and the topic, the row opened, the graph's entity, and a voice to match (`voice_doc` with
 * `voice_turn`, `voice_speaker` or `voice_t`). The panels the link names are the page's to bring in front, over
 * the layout it restores: the link is answered for that.
 */
import type { SearchMode } from '@workbench/explorer-api';
import { explore } from '#lib/explore/explore.svelte.ts';
import { voiceSearch } from '#lib/explore/voice-search.svelte.ts';
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
	if (link.row !== null) explore.openLinked(link.row, link.t);
	else if (link.doc !== null)
		explore.active = explore.playerRow(link.doc, link.t ?? 0, link.t ?? 0);
	return link;
}
