/**
 * Which panes the right-hand viewer may show for the active corpus.
 *
 * The defect this exists to prevent: viewing a still IMAGE offered a time scrubber, a Transcript tab
 * and a Speakers tab. The media ELEMENT was already chosen from the corpus's declared kind — an image
 * corpus correctly got an `<img>` and not a `<video>` — but the chrome AROUND it was wrapped in
 * `{#if !isFullscreen}` and nothing else, so every corpus got the full audio/video apparatus whether
 * or not one word of it applied.
 *
 * Gate on what the dataset DECLARES, using the capability names the SERVICE already reads, so a declaration cannot mean one thing to the UI and another to the endpoint
 * behind it.
 *
 *   timeline    <- the corpus is time-based at all
 *   Transcript  <- `alignments`   (what `transcripts.py` looks up for the alignment table)
 *   Speakers    <- `diarization`  (what `diarization.py` looks up for speaker turns)
 *
 * Pure, so "what does an image corpus offer" is answerable without a browser.
 */

/** The subset of `DatasetView` this reads, declared structurally so the module imports no client. */
export interface ViewerCapabilities {
	isPlayable: boolean;
	hasCapability(name: string): boolean;
}

export interface Panes {
	/** The chunk scrubber under the media. Meaningless without a time axis. */
	timeline: boolean;
	transcript: boolean;
	speakers: boolean;
	/** Whether the tab strip is worth drawing at all — one tab is a label, not a choice. */
	tabs: boolean;
}

export function panesFor(view: ViewerCapabilities | null): Panes {
	// Null view = the descriptor has not landed. Nothing conditional, matching the sidebar's
	// fail-closed posture: showing a Transcript tab that may vanish is worse than showing it late.
	if (view === null) return { timeline: false, transcript: false, speakers: false, tabs: false };

	const transcript = view.hasCapability('alignments');
	const speakers = view.hasCapability('diarization');
	return {
		timeline: view.isPlayable,
		transcript,
		speakers,
		// Both gone means no strip. Note this is NOT `transcript || speakers` collapsed away: a corpus
		// with exactly one of them still gets the strip, because the strip is also where the
		// fullscreen toggle lives.
		tabs: transcript || speakers,
	};
}

/**
 * The tab actually shown, given what the user picked and what the corpus offers.
 *
 * Derived rather than corrected: clamping by ASSIGNING `tab` inside an effect is the anti-pattern
 * that makes this kind of coupling invisible, and it would also fight the user — switching corpora
 * would silently rewrite a choice they could not see being rewritten.
 */
export function shownTab(
	picked: 'transcript' | 'speakers',
	panes: Panes,
): 'transcript' | 'speakers' | null {
	if (picked === 'speakers' && panes.speakers) return 'speakers';
	if (picked === 'transcript' && panes.transcript) return 'transcript';
	// The pick is unavailable here — fall back to whichever the corpus does declare.
	if (panes.transcript) return 'transcript';
	if (panes.speakers) return 'speakers';
	return null;
}

/**
 * What a dataset with no workbench descriptor offers, read from one table's columns (the viewer's
 * `ColumnView`: its kind and the indices the table itself carries). Nothing is guessed: text search
 * needs a full-text index, similar rows a vector index, the atlas a declared projection (which a plain
 * table has none of), and annotation a lakehouse's dataset with a descriptor naming its rows' identity.
 * Each capability the table lacks carries the reason a disabled control shows.
 */
export interface PlainColumn {
	name: string;
	kind: string;
	text_search: boolean;
	similar: boolean;
}

export interface PlainPanes {
	/** The column a text search runs over, or why there is none. */
	search: { column: string } | { why: string };
	/** The vector column similar rows are ranked by, or why there is none. */
	similar: { column: string } | { why: string };
	/** Why the atlas cannot be drawn. */
	atlas: string;
	/** Why the rows cannot be sent to a project. */
	send: string;
}

export function plainPanes(columns: readonly PlainColumn[], readOnly: string | null): PlainPanes {
	const searchable = columns.find((c) => c.text_search);
	const similar = columns.find((c) => c.similar);
	const texts = columns.some((c) => c.kind === 'text');
	const vectors = columns.some((c) => c.kind === 'vector');
	return {
		search: searchable
			? { column: searchable.name }
			: {
					why: texts
						? 'no text column of this table carries a full-text index'
						: 'this table has no text column',
				},
		similar: similar
			? { column: similar.name }
			: {
					why: vectors
						? 'no vector column of this table carries a vector index'
						: 'this table has no vector column',
				},
		atlas: 'this dataset declares no embedding projection to draw',
		send:
			readOnly ??
			'this dataset declares no workbench descriptor, so its rows have no identity a task can name',
	};
}

/**
 * The dock panels a dataset can feed, and why each other one cannot: the one place a panel's gate is
 * decided, read by the dock's "open a panel" menu (an unavailable panel is listed disabled with this
 * reason, never hidden), by a panel already open on a dataset that cannot feed it (it shows the same
 * reason inside), and by the default layout (which opens only the panels the dataset can feed).
 *
 * The reasons are phrased about the dataset ("declares no knowledge graph") rather than the product: an
 * absent entry reads as a feature the workbench lacks, a disabled one says what to declare. The gates read
 * the names the viewer itself reads: `graph` is the capability its graph door looks up, `topics` the one its
 * topics door looks up, and the atlas gates on the declared spaces its atlas door draws.
 */
export type DockPanel = 'results' | 'player' | 'atlas' | 'graph' | 'tree';

/** A panel's gate: null when the dataset can feed it, else why not. */
export type PanelGates = Readonly<Record<DockPanel, string | null>>;

/** The subset of `DatasetView` the panel gates read, declared structurally so the module imports no client. */
export interface PanelCapabilities {
	hasCapability(name: string): boolean;
	atlasSpaces: readonly { table: string }[];
}

/** The reason every descriptor-only panel gives on a dataset shown as a plain table. */
const NO_DESCRIPTOR =
	'this dataset carries no workbench descriptor, so it is shown as a plain table';

/**
 * `view` is the stamped dataset's view, or null while its descriptor is read; `plain` is a dataset shown
 * as a plain table (no descriptor), whose atlas reason is `plainPanes`'s. `rowTable` is the table in view:
 * an atlas space is bound to one table, so a space over `pages` has nothing to draw while the reader
 * searches `lines`; with no table chosen, any declared space feeds it.
 */
export function panelGates(
	view: PanelCapabilities | null,
	{ rowTable = null, plain = false }: { rowTable?: string | null; plain?: boolean } = {},
): PanelGates {
	if (plain) {
		return {
			results: null,
			player: null,
			atlas: plainPanes([], null).atlas,
			graph: NO_DESCRIPTOR,
			tree: NO_DESCRIPTOR,
		};
	}
	if (view === null) {
		const reading = 'the dataset is still being read';
		return { results: null, player: null, atlas: reading, graph: reading, tree: reading };
	}
	const atlas =
		rowTable === null
			? view.atlasSpaces.length > 0
			: view.atlasSpaces.some((space) => space.table === rowTable);
	return {
		results: null,
		player: null,
		atlas: atlas
			? null
			: view.atlasSpaces.length === 0
				? 'this dataset declares no atlas projection'
				: 'this dataset declares no atlas projection for the table in view',
		graph: view.hasCapability('graph') ? null : 'this dataset declares no knowledge graph',
		// The schema, not whether the hierarchy has been computed: a declared tree that is not built yet
		// opens, and its panel says none has been written.
		tree: view.hasCapability('topics') ? null : 'this dataset declares no topic hierarchy',
	};
}

/**
 * Why a dataset's rows cannot be sent to a project, or null when they can: the one gate every "Send to a
 * project" control reads, for a stamped dataset (its source's read-only reason, `facts.read_only`) and a
 * plain one (which also lacks a descriptor naming its rows' identity, `plainPanes`).
 */
export function sendGate(
	facts: { stamped: boolean; read_only: string | null } | null,
): string | null {
	if (facts === null) return null;
	return facts.stamped ? facts.read_only : plainPanes([], facts.read_only).send;
}
