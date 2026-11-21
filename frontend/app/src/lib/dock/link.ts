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
 * - `panel`, once per panel shown, each brought in front when the link is opened; `max`: the panel maximized;
 * - a table nobody stamped: the Atlas, Graph and Tree set-ups (`#lib/plain/setup.ts`, `ax`, `gs`, `tn`, …), so a
 *   link draws what its sender drew on a browser that never saved it, and `node` with `nodeof` (`graph` or
 *   `tree`): the node whose rows the Rows panel lists. A set-up that does not validate, or is longer than a
 *   link should carry, is left out and `setupProblem` says why.
 *
 * A list is the parameter repeated rather than one value joined by a separator, so no value needs a
 * separator escaped and the query stays as `URLSearchParams` writes it.
 *
 * Every other parameter (`project`, `table`) is the page's own and is kept as it is. Pure, so the round
 * trip is testable without a browser.
 */
import { readSetups, SETUP_PARAMS, writeSetups, type Setups } from '#lib/plain/setup.ts';
import { isPanelId, type PanelId } from './layout.ts';

/** The node a plain table's rows are filtered to: picked in its graph or drilled in its tree. */
export interface LinkedNode {
	of: 'graph' | 'tree';
	id: string;
}

/** The longest node key a link carries: the viewer's own bound on a tree node's key (`TreeNodeQuery.key`), the
 *  longer of its two (a graph node's typed key is at most 258 characters). */
const MAX_NODE = 3072;

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
	setups: Setups;
	node: LinkedNode | null;
	/** Why a link's set-up was left out; never written. */
	setupProblem?: string | null;
}

/** The parameters this module owns; every other one is left as it is. */
const OWNED = [
	'q',
	'mode',
	'topic',
	'row',
	'doc',
	't',
	'entity',
	'panel',
	'max',
	'node',
	'nodeof',
	...SETUP_PARAMS,
] as const;

/** An entity id as the graph doors take it: 16 lowercase hex characters. */
const ENTITY_ID = /^[0-9a-f]{16}$/;

const text = (value: string | null): string | null => (value ? value : null);

export function readDockLink(params: Pick<URLSearchParams, 'get' | 'getAll' | 'has'>): DockLink {
	const t = Number(params.get('t'));
	const row = params.getAll('row');
	const panels = params.getAll('panel').filter((id): id is PanelId => isPanelId(id));
	const max = params.get('max');
	const entity = params.get('entity');
	const { setups, problem } = readSetups(params);
	const node = params.get('node');
	const nodeOf = params.get('nodeof');
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
		setups,
		node:
			node !== null && node.length <= MAX_NODE && (nodeOf === 'graph' || nodeOf === 'tree')
				? { of: nodeOf, id: node }
				: null,
		setupProblem: problem,
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
	writeSetups(out, link.setups);
	if (link.node !== null) {
		out.set('node', link.node.id);
		out.set('nodeof', link.node.of);
	}
	return out;
}
