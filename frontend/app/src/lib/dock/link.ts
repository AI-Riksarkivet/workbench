/**
 * A dataset page's link: what a person sees, written into the page's address so the link shows the same
 * to whoever opens it, and a reload shows it again. The dock's arrangement itself is the person's (kept in
 * this browser, `#lib/dock/store.ts`); the link carries what it must for another person to land on the
 * same view, and nothing that is a credential or a preference:
 *
 * - `q`, `mode`, `topic`: the search (a topic is the tree's selection);
 * - `row`, once per key field in the descriptor's order (the opened row's key), and `t` (the moment of a
 *   recording it opened at); `doc` with `t` names a moment of a document alone;
 * - `entity`: the knowledge graph's selected entity;
 * - `panel`, once per panel shown, each brought in front when the link is opened; `max`: the panel maximized.
 *
 * A list is the parameter repeated rather than one value joined by a separator, so no value needs a
 * separator escaped and the query stays as `URLSearchParams` writes it.
 *
 * Every other parameter (`project`, `table`, the voice search's `voice_*`) is the page's own and is kept
 * as it is. Pure, so the round trip is testable without a browser.
 */
import { isPanelId, type PanelId } from './layout.ts';

export interface DockLink {
	q: string | null;
	mode: string | null;
	topic: string | null;
	/** The opened row's key values, in the descriptor's key-field order. */
	row: string[] | null;
	doc: string | null;
	t: number | null;
	entity: string | null;
	panels: PanelId[];
	max: PanelId | null;
}

/** The parameters this module owns; every other one is left as it is. */
const OWNED = ['q', 'mode', 'topic', 'row', 'doc', 't', 'entity', 'panel', 'max'] as const;

/** An entity id as the graph doors take it: 16 lowercase hex characters. */
const ENTITY_ID = /^[0-9a-f]{16}$/;

const text = (value: string | null): string | null => (value ? value : null);

export function readDockLink(params: Pick<URLSearchParams, 'get' | 'getAll' | 'has'>): DockLink {
	const t = Number(params.get('t'));
	const row = params.getAll('row');
	const panels = params.getAll('panel').filter((id): id is PanelId => isPanelId(id));
	const max = params.get('max');
	const entity = params.get('entity');
	return {
		q: text(params.get('q')),
		mode: text(params.get('mode')),
		topic: text(params.get('topic')),
		row: row.length > 0 ? row : null,
		doc: text(params.get('doc')),
		t: params.has('t') && Number.isFinite(t) ? Math.max(0, t) : null,
		entity: entity && ENTITY_ID.test(entity) ? entity : null,
		panels: [...new Set(panels)],
		max: max !== null && isPanelId(max) ? max : null,
	};
}

/** `params` with this module's parameters replaced by `link`'s; the page's own are kept, in their order. */
export function writeDockLink(params: URLSearchParams, link: DockLink): URLSearchParams {
	const out = new URLSearchParams(params);
	for (const name of OWNED) out.delete(name);
	const set = (name: string, value: string | null): void => {
		if (value !== null && value !== '') out.set(name, value);
	};
	set('q', link.q);
	set('mode', link.mode);
	set('topic', link.topic);
	for (const key of link.row ?? []) out.append('row', key);
	set('doc', link.row === null ? link.doc : null);
	set('t', link.t === null ? null : String(Math.round(link.t * 100) / 100));
	set('entity', link.entity);
	for (const panel of link.panels) out.append('panel', panel);
	set('max', link.max);
	return out;
}
