/**
 * The dataset's knowledge graph as the Graph panel explores it: the subgraph laid out (the overview, or one
 * entity's neighbourhood), the entity selected with its clips and neighbours, the trail of entities visited,
 * the entity search and the Cypher console.
 *
 * A module and not the panel's state, because the dock remounts a panel it moves, maximizes or pops out
 * (`#lib/dock/Dock.svelte`'s panel rule): a maximized graph shows the entity it showed docked. `open` starts
 * another dataset afresh.
 *
 * THE SELECTION IS SHARED. Selecting an entity searches the dataset for its name (`explore`), so the Results
 * panel lists the rows that mention it and the Atlas lights them up; entities are linked to the text by name
 * only (the graph's own help says so), and a text search is the one door that reads rows by what they say.
 * A clip opens in the Player panel. Reads go through `#lib/api/graph.api.ts`, as the person; only the latest
 * load writes the state, so an answer for an entity the reader has left is dropped.
 */
import {
	forceCollide,
	forceLink,
	forceManyBody,
	forceSimulation,
	forceX,
	forceY,
	type SimulationNodeDatum,
} from 'd3-force';
import type {
	GraphCypherResponse,
	GraphEntityResponse,
	GraphMatch,
	GraphNode,
} from '@workbench/explorer-api';
import { activeView } from '@workbench/explorer-api/descriptor';
import { graphEntity, graphMatches, graphSubgraph, runCypher } from '#lib/api/graph.api.ts';
import { dockControl } from '#lib/dock/dock-control.svelte.ts';
import { explore } from '#lib/explore/explore.svelte.ts';
import { failureText } from '#lib/failure.ts';

export type GraphView = 'graph' | 'table' | 'json';
type SimNode = SimulationNodeDatum & { id: string; idx: number };

/** A node's size on the canvas, by how many clips mention it. */
const sizeOf = (mentions: number): number => 8 + Math.min(20, Math.sqrt(Math.max(1, mentions)) * 4);

class GraphExplorer {
	/** The dataset this state is for. */
	dataset = $state<string | null>(null);
	view = $state<GraphView>('graph');
	loading = $state(false);
	/** Why the last subgraph or entity could not be read. */
	failure = $state<{ what: string; detail: string } | null>(null);
	showHelp = $state(false);

	searchQ = $state('');
	matches = $state.raw<GraphMatch[]>([]);
	dropdownOpen = $state(false);

	cypherText = $state('');
	cypherOpen = $state(false);
	cypherResult = $state.raw<GraphCypherResponse | null>(null);
	cypherRunning = $state(false);

	detail = $state.raw<GraphEntityResponse | null>(null);
	/** Whether the selected entity's panel is open; closing it keeps the selection. */
	detailOpen = $state(false);
	selectedId = $state<string | null>(null);
	/** The trail the breadcrumb shows; empty is the overview. */
	trail = $state.raw<{ id: string; name: string }[]>([]);

	// The force layout's buffers; `layoutVersion` is the GPU's signal that they changed.
	nodes = $state.raw<GraphNode[]>([]);
	idToIdx = $state.raw<Map<string, number>>(new Map());
	nodesX = $state.raw(new Float32Array(0));
	nodesY = $state.raw(new Float32Array(0));
	nodeSize = $state.raw(new Float32Array(0));
	nodeState = $state.raw(new Uint8Array(0));
	edgesIdx = $state.raw(new Uint32Array(0));
	layoutVersion = $state(0);
	fitBounds = $state.raw({ minX: -1, minY: -1, maxX: 1, maxY: 1 });

	#loadSeq = 0;
	#cypherSeq = 0;
	#searchTimer: ReturnType<typeof setTimeout> | undefined;
	/** The load the reader last asked for, which "Try again" repeats. */
	#lastLoad: () => Promise<void> = () => this.loadOverview();
	/** The dataset whose first layout was asked for: it is asked once, and a failure waits for Try again. */
	#attempted: string | null = null;

	/**
	 * Start `dataset` afresh, with `entity` selected when a link names one. For the dataset already open, a
	 * link naming another entity selects it (its search is the link's own, so the results are left to it).
	 */
	open(dataset: string, entity: string | null = null): void {
		if (this.dataset === dataset) {
			if (entity !== null && entity !== this.selectedId) {
				this.selectedId = entity;
				if (this.#attempted === dataset) void this.selectEntity(entity, { filter: false });
			}
			return;
		}
		this.#attempted = null;
		this.#loadSeq++;
		this.#cypherSeq++;
		clearTimeout(this.#searchTimer);
		this.dataset = dataset;
		this.view = 'graph';
		this.loading = false;
		this.failure = null;
		this.searchQ = '';
		this.matches = [];
		this.dropdownOpen = false;
		this.cypherText = '';
		this.cypherOpen = false;
		this.cypherResult = null;
		this.cypherRunning = false;
		this.detail = null;
		this.detailOpen = false;
		this.selectedId = entity;
		this.trail = [];
		this.#layout([], []);
	}

	/**
	 * Lay the dataset out once its graph is built: the selected entity's neighbourhood, else the overview.
	 * Asked once per dataset: a panel remounted draws what is held, and a read that failed is asked again
	 * only when the person presses Try again (`retry`).
	 */
	ensureLoaded(): void {
		const dataset = this.dataset;
		if (dataset === null || this.#attempted === dataset) return;
		this.#attempted = dataset;
		if (this.selectedId !== null) void this.selectEntity(this.selectedId, { filter: false });
		else void this.loadOverview();
	}

	/** The person's Try again: the read that failed is asked once more. */
	retry(): Promise<void> {
		return this.#lastLoad();
	}

	async loadOverview(): Promise<void> {
		const dataset = this.dataset;
		if (dataset === null) return;
		const mine = ++this.#loadSeq;
		this.#lastLoad = () => this.loadOverview();
		this.loading = true;
		this.failure = null;
		try {
			const sub = await graphSubgraph({ dataset, entityId: null, limit: 120 });
			if (mine !== this.#loadSeq) return;
			this.selectedId = null;
			this.detail = null;
			this.detailOpen = false;
			this.trail = [];
			this.#layout(sub.nodes, sub.edges);
		} catch (err) {
			if (mine === this.#loadSeq)
				this.failure = { what: 'The graph could not be loaded.', detail: failureText(err) };
		} finally {
			if (mine === this.#loadSeq) this.loading = false;
		}
	}

	/**
	 * Select an entity: its neighbourhood is laid out, its panel opens, and (unless `filter` is off, as when
	 * a link restores the selection beside its own search) the results search for its name.
	 */
	async selectEntity(id: string, { filter = true }: { filter?: boolean } = {}): Promise<void> {
		const dataset = this.dataset;
		if (dataset === null) return;
		const mine = ++this.#loadSeq;
		this.#lastLoad = () => this.selectEntity(id, { filter });
		this.dropdownOpen = false;
		this.view = 'graph';
		this.loading = true;
		this.failure = null;
		try {
			const [sub, d] = await Promise.all([
				graphSubgraph({ dataset, entityId: id, limit: 60 }),
				graphEntity({ dataset, entityId: id }),
			]);
			if (mine !== this.#loadSeq) return;
			this.selectedId = id;
			this.detail = d;
			this.detailOpen = true;
			// Revisiting a crumb truncates the trail back to it; a new hop appends.
			const at = this.trail.findIndex((c) => c.id === id);
			const label = d.entity?.name ?? id;
			this.trail = at >= 0 ? this.trail.slice(0, at + 1) : [...this.trail, { id, name: label }];
			this.#layout(sub.nodes, sub.edges);
			if (filter && d.entity) this.#searchFor(d.entity.name);
		} catch (err) {
			if (mine === this.#loadSeq)
				this.failure = { what: 'This entity could not be loaded.', detail: failureText(err) };
		} finally {
			if (mine === this.#loadSeq) this.loading = false;
		}
	}

	/** The results list the rows that name the entity, and the Results panel comes in front. */
	#searchFor(name: string): void {
		const view = activeView();
		if (!view.hasMode('fts')) return;
		explore.apply({ ...explore.spec, q: name, topic: undefined, image: undefined, mode: 'fts' });
		// A maximized graph lists the rows itself, as the page it stands in for did.
		if (dockControl.maximized !== 'graph') dockControl.reveal('results');
	}

	/** Open a clip in the Player panel, at the moment it mentions the entity, titled and worded as the clip is. */
	playClip(clip: {
		doc_id: string;
		start: number;
		end: number;
		title: string;
		text: string;
	}): void {
		const view = activeView();
		// The row the results already hold for this clip (the search for the entity found it) is opened as it
		// is: a page corpus's clip names its document, not which of its pages, and the hit names both.
		const held = explore.hits.find(
			(hit) => view.docId(hit) === clip.doc_id && view.body(hit) === clip.text,
		);
		if (held) {
			explore.active = held;
			this.#showPlayer();
			return;
		}
		const shown: Record<string, unknown> = {};
		const titleField = view.titleFields[0];
		if (titleField) shown[titleField] = clip.title;
		if (view.bodyField) shown[view.bodyField] = clip.text;
		explore.active = explore.playerRow(clip.doc_id, clip.start, clip.end, {
			...shown,
			_score: 0,
			alignments: [],
		});
		this.#showPlayer();
	}

	/** A maximized graph plays the row beside it; docked, the Player panel comes in front. */
	#showPlayer(): void {
		if (dockControl.maximized !== 'graph') dockControl.reveal('player');
	}

	async runQuery(): Promise<void> {
		const dataset = this.dataset;
		const q = this.cypherText.trim();
		if (!q || dataset === null) return;
		this.cypherOpen = true;
		this.view = 'table';
		this.cypherRunning = true;
		const mine = ++this.#cypherSeq;
		const res = await runCypher(dataset, q);
		if (mine !== this.#cypherSeq) return;
		this.cypherRunning = false;
		// A refused or unreachable console call shows in the table as the result's own error.
		this.cypherResult = res.ok
			? res.data
			: { built: false, columns: [], rows: [], error: res.detail };
	}

	runExample(query: string): void {
		this.cypherText = query;
		this.cypherOpen = true;
		void this.runQuery();
	}

	/** The entity search: a match list for two letters or more, a quarter second after the last key. */
	searchInput(): void {
		const dataset = this.dataset;
		const q = this.searchQ.trim();
		clearTimeout(this.#searchTimer);
		if (q.length < 2 || dataset === null) {
			this.matches = [];
			this.dropdownOpen = false;
			return;
		}
		this.#searchTimer = setTimeout(() => {
			graphMatches({ dataset, q }).then(
				(r) => {
					if (this.searchQ.trim() !== q || this.dataset !== dataset) return;
					this.matches = r.matches;
					this.dropdownOpen = true;
				},
				() => {
					if (this.searchQ.trim() !== q || this.dataset !== dataset) return;
					this.matches = [];
					this.dropdownOpen = false;
				},
			);
		}, 250);
	}

	/** Fit the camera to the nodes again. */
	recomputeFit(): void {
		const xs = this.nodesX;
		const ys = this.nodesY;
		const n = xs.length;
		if (n === 0) return;
		let minX = Infinity;
		let minY = Infinity;
		let maxX = -Infinity;
		let maxY = -Infinity;
		for (let i = 0; i < n; i++) {
			minX = Math.min(minX, xs[i]!);
			minY = Math.min(minY, ys[i]!);
			maxX = Math.max(maxX, xs[i]!);
			maxY = Math.max(maxY, ys[i]!);
		}
		const padX = (maxX - minX) * 0.08 || 1;
		const padY = (maxY - minY) * 0.08 || 1;
		this.fitBounds = { minX: minX - padX, minY: minY - padY, maxX: maxX + padX, maxY: maxY + padY };
	}

	/**
	 * Lay `nodes` out. The force layout settles synchronously (milliseconds at these node counts), so the
	 * nodes mount in their final places and the camera fits once, not three times while a live simulation
	 * moves.
	 */
	#layout(nodes: GraphNode[], edges: { source: string; target: string }[]): void {
		const n = nodes.length;
		const idx = new Map(nodes.map((nd, i) => [nd.id, i]));
		const x = new Float32Array(n);
		const y = new Float32Array(n);
		const size = new Float32Array(n);
		const R = 40 + n * 1.5;
		for (let i = 0; i < n; i++) {
			const a = (i / Math.max(1, n)) * Math.PI * 2;
			x[i] = Math.cos(a) * R;
			y[i] = Math.sin(a) * R;
			size[i] = sizeOf(nodes[i]!.mentions);
		}
		const valid = edges.filter((e) => idx.has(e.source) && idx.has(e.target));
		const ei = new Uint32Array(valid.length * 2);
		valid.forEach((e, i) => {
			ei[i * 2] = idx.get(e.source)!;
			ei[i * 2 + 1] = idx.get(e.target)!;
		});

		const simNodes: SimNode[] = nodes.map((nd, i) => ({ id: nd.id, idx: i, x: x[i], y: y[i] }));
		if (n > 0) {
			const links = valid.map((e) => ({ source: e.source, target: e.target }));
			const sim = forceSimulation<SimNode>(simNodes)
				.stop()
				.force('charge', forceManyBody<SimNode>().strength(-220))
				.force(
					'link',
					forceLink<SimNode, { source: string; target: string }>(links)
						.id((d) => d.id)
						.distance(46)
						.strength(0.35),
				)
				.force(
					'collide',
					forceCollide<SimNode>((d) => sizeOf(nodes[d.idx]?.mentions ?? 1) / 2 + 5),
				)
				.force('x', forceX<SimNode>(0).strength(0.04))
				.force('y', forceY<SimNode>(0).strength(0.04));
			for (let tick = 0; sim.alpha() > 0.025 && tick < 400; tick++) sim.tick();
			sim.stop();
			for (let i = 0; i < n; i++) {
				x[i] = simNodes[i]!.x ?? 0;
				y[i] = simNodes[i]!.y ?? 0;
			}
		}
		this.nodes = nodes;
		this.idToIdx = idx;
		this.nodesX = x;
		this.nodesY = y;
		this.nodeSize = size;
		this.nodeState = new Uint8Array(n);
		this.edgesIdx = ei;
		this.recomputeFit();
		this.layoutVersion++;
	}
}

export const graphExplorer = new GraphExplorer();
