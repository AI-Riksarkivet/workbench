/**
 * Which panes a corpus may show.
 *
 * The reported defect: viewing a still IMAGE showed a time scrubber, a Transcript tab and a Speakers
 * tab. The media ELEMENT was already chosen from the declared kind — an image corpus correctly got an
 * `<img>` — but the chrome around it sat inside `{#if !isFullscreen}` and nothing else, so every
 * corpus got the whole audio/video apparatus whether or not a word of it applied.
 *
 * The gates use the capability names the SERVICE reads (`transcripts.py` looks up `alignments`,
 * `diarization.py` looks up `diarization`), so a declaration cannot mean one thing to the UI and
 * another to the endpoint behind it.
 */

import { DatasetDescriptorSchema, DatasetView } from '@workbench/explorer-api/descriptor';
import * as v from 'valibot';
import { describe, expect, it } from 'vitest';

import {
	panelGates,
	panesFor,
	plainPanes,
	shownTab,
	type PanelGates,
	type ViewerCapabilities,
} from './pane-capabilities.ts';

const view = (isPlayable: boolean, capabilities: string[] = []): ViewerCapabilities => ({
	isPlayable,
	hasCapability: (name) => capabilities.includes(name),
});

const IMAGE_CORPUS = view(false);
const AV_CORPUS = view(true, ['alignments', 'diarization']);

describe('what an IMAGE corpus offers', () => {
	it('offers no timeline, no transcript and no speakers', () => {
		// The reported bug, stated directly.
		expect(panesFor(IMAGE_CORPUS)).toEqual({
			timeline: false,
			transcript: false,
			speakers: false,
			tabs: false,
		});
	});

	it('shows NO sync body at all rather than an empty one', () => {
		// An empty panel reads as a failed load; absence reads as "not applicable".
		expect(shownTab('transcript', panesFor(IMAGE_CORPUS))).toBeNull();
	});
});

describe('the gates are independent', () => {
	it('a time axis alone does not conjure a transcript', () => {
		// A video corpus with no alignment table: scrubbing is meaningful, karaoke is not.
		const panes = panesFor(view(true));

		expect(panes.timeline).toBe(true);
		expect(panes.transcript).toBe(false);
		expect(panes.speakers).toBe(false);
		expect(panes.tabs).toBe(false);
	});

	it('alignments without diarization offers Transcript only', () => {
		const panes = panesFor(view(true, ['alignments']));

		expect([panes.transcript, panes.speakers]).toEqual([true, false]);
		expect(panes.tabs).toBe(true);
	});

	it('diarization without alignments offers Speakers only', () => {
		const panes = panesFor(view(true, ['diarization']));

		expect([panes.transcript, panes.speakers]).toEqual([false, true]);
		expect(panes.tabs).toBe(true);
	});

	it('a capability for something ELSE opens nothing', () => {
		// `frames` is what the seeded corpus actually declares.
		expect(panesFor(view(true, ['frames']))).toEqual({
			timeline: true,
			transcript: false,
			speakers: false,
			tabs: false,
		});
	});

	it('a fully-declared corpus offers everything', () => {
		expect(panesFor(AV_CORPUS)).toEqual({
			timeline: true,
			transcript: true,
			speakers: true,
			tabs: true,
		});
	});
});

describe('before a descriptor exists', () => {
	it('offers nothing rather than everything', () => {
		expect(panesFor(null)).toEqual({
			timeline: false,
			transcript: false,
			speakers: false,
			tabs: false,
		});
	});
});

describe('which tab is actually shown', () => {
	it('honours the pick when the corpus supports it', () => {
		expect(shownTab('speakers', panesFor(AV_CORPUS))).toBe('speakers');
		expect(shownTab('transcript', panesFor(AV_CORPUS))).toBe('transcript');
	});

	it('falls back when the pick is unavailable HERE', () => {
		// Someone on Speakers switches to a corpus with only alignments. The pick is untouched — this
		// is derived, not a corrected `tab` — so switching back restores it.
		expect(shownTab('speakers', panesFor(view(true, ['alignments'])))).toBe('transcript');
		expect(shownTab('transcript', panesFor(view(true, ['diarization'])))).toBe('speakers');
	});
});

describe('what a table with no descriptor offers', () => {
	const column = (name: string, kind: string, text_search = false, similar = false) => ({
		name,
		kind,
		text_search,
		similar,
	});

	it('searches and ranks only where the table carries the index', () => {
		const panes = plainPanes(
			[column('note', 'text', true), column('embedding', 'vector', false, true)],
			null,
		);
		expect([panes.search, panes.similar]).toEqual([{ column: 'note' }, { column: 'embedding' }]);
	});

	it('says why a column of the kind is not enough, and why there is none', () => {
		const panes = plainPanes([column('note', 'text'), column('n', 'integer')], 'read-only source');
		expect(panes).toEqual({
			search: { why: 'no text column of this table carries a full-text index' },
			similar: { why: 'this table has no vector column' },
			atlas: 'this dataset declares no embedding projection to draw',
			send: 'read-only source',
		});
	});
});

// ── the dock's panels, gated on what the dataset declares, asserted against real descriptors parsed by the
// real schema ──

/** The minimum a descriptor must carry; everything conditional is added per test. */
function descriptor(extra: Record<string, unknown> = {}): unknown {
	return {
		id: 'corpus',
		tables: {
			items: {
				name: 'items',
				row_count: 1,
				version: 1,
				columns: [{ name: 'item_id', arrow_type: 'string', nullable: false }],
				indexes: [],
			},
		},
		declared: {
			identity: { key_fields: ['item_id'], doc_key: 'item_id', doc_key_pattern: '.*' },
			document: { table: 'items', media_blob: 'blob', mime: 'mime' },
			time: null,
			display: { title: ['label'], body: 'caption', caption: null, metadata: [] },
			search: {
				row_table: 'items',
				fts: { table: 'items', column: 'caption', language: 'English' },
				vectors: {},
				filterable: [],
				rerank: false,
			},
			atlas: [],
			capabilities: {},
			...extra,
		},
	};
}

const stamped = (extra: Record<string, unknown> = {}): DatasetView =>
	new DatasetView(v.parse(DatasetDescriptorSchema, descriptor(extra)));

const ATLAS_SPACE = {
	name: 'text',
	x: 'atlas_x',
	y: 'atlas_y',
	cluster: 'atlas_cluster',
	source_column: 'semantic_vec',
	table: 'items',
	channels: [],
};

type ViewPanel = 'atlas' | 'graph' | 'tree';
const VIEW_PANELS: readonly ViewPanel[] = ['atlas', 'graph', 'tree'];

const openable = (gates: PanelGates): ViewPanel[] => VIEW_PANELS.filter((p) => gates[p] === null);

const why = (gates: PanelGates, panel: ViewPanel): string | null => gates[panel];

describe('the view panels follow what the dataset declares', () => {
	it('feeds the results and the player always, and no view panel on a dataset that declares nothing', () => {
		const gates = panelGates(stamped());
		expect([gates.results, gates.player]).toEqual([null, null]);
		expect(openable(gates)).toEqual([]);
	});

	it('opens each view by its own declaration, and a capability for something else opens none', () => {
		expect(openable(panelGates(stamped({ atlas: [ATLAS_SPACE] })))).toEqual(['atlas']);
		expect(openable(panelGates(stamped({ capabilities: { graph: 'kg' } })))).toEqual(['graph']);
		expect(openable(panelGates(stamped({ capabilities: { topics: 'topics.tree' } })))).toEqual([
			'tree',
		]);
		expect(openable(panelGates(stamped({ capabilities: { frames: 'chunks.image' } })))).toEqual([]);
	});

	it('a closed view says why, about the dataset; an open one carries no reason', () => {
		const closed = panelGates(stamped());
		expect(why(closed, 'graph')).toMatch(/dataset declares no knowledge graph/);
		expect(why(closed, 'tree')).toMatch(/topic/);
		expect(why(closed, 'atlas')).toMatch(/atlas/);
		expect(why(panelGates(stamped({ capabilities: { graph: 'kg' } })), 'graph')).toBeNull();
	});

	it('while the descriptor is read, every view is closed as loading', () => {
		const entries = panelGates(null);
		expect(openable(entries)).toEqual([]);
		expect(why(entries, 'graph')).toMatch(/still being read/);
	});
});

describe('the atlas follows the table in view', () => {
	const spaceOn = (table: string) => ({ ...ATLAS_SPACE, table });

	it('opens when a space is bound to that table, or to any table when none is chosen', () => {
		expect(
			openable(panelGates(stamped({ atlas: [spaceOn('pages')] }), { rowTable: 'pages' })),
		).toContain('atlas');
		expect(
			openable(panelGates(stamped({ atlas: [spaceOn('pages')] }), { rowTable: null })),
		).toContain('atlas');
	});

	it('closes, naming the table, when every space belongs to another table', () => {
		const entries = panelGates(stamped({ atlas: [spaceOn('pages')] }), { rowTable: 'lines' });
		expect(openable(entries)).not.toContain('atlas');
		expect(why(entries, 'atlas')).toMatch(/table/);
	});

	it('leaves the graph and the tree to the dataset: capabilities are declared once per dataset', () => {
		const declared = stamped({ capabilities: { topics: 'topics.tree', graph: 'kg' } });
		expect(openable(panelGates(declared, { rowTable: 'pages' }))).toEqual(['graph', 'tree']);
	});
});

describe('a dataset shown as a plain table', () => {
	it('feeds its rows and its row, and every view panel says the dataset carries no descriptor', () => {
		const gates = panelGates(null, { plain: true });
		expect([gates.results, gates.player]).toEqual([null, null]);
		expect(gates.atlas).toMatch(/no embedding projection/);
		expect(gates.graph).toMatch(/no workbench descriptor/);
		expect(gates.tree).toMatch(/no workbench descriptor/);
	});
});
