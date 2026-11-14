<script lang="ts">
	/**
	 * The dataset's knowledge graph: an entity search, Cypher presets and a console drive a result area
	 * with three views, the graph (a d3-force layout drawn by the WebGPU `<GpuGraph>`), a table and JSON.
	 * Picking a node opens its panel: properties, the clips that mention it (each opens the dataset's
	 * player at that moment) and the related and co-occurring entities, which re-centre the graph.
	 *
	 * Reads go through `#lib/api/graph.api.ts`, as the person. The graph view needs WebGPU; the table and
	 * JSON views and the entity panel do not, so a browser without it still reads the graph.
	 *
	 * Narrow (the page's own width, a container query), the side panels cover the result area instead of
	 * squeezing it, and the example queries stack under it.
	 */
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import {
		forceCollide,
		forceLink,
		forceManyBody,
		forceSimulation,
		forceX,
		forceY,
		type Simulation,
		type SimulationNodeDatum,
	} from 'd3-force';
	import { HelpCircle, X } from '@lucide/svelte';
	import type {
		GraphCypherResponse,
		GraphEntityResponse,
		GraphMatch,
		GraphNode,
	} from '@workbench/explorer-api';
	import { Button } from '@workbench/ui';
	import { Input } from '@workbench/ui/input';
	import { Select, type SelectOption } from '@workbench/ui/select';
	import {
		graphEntity,
		graphMatches,
		graphStatus,
		graphSubgraph,
		runCypher,
	} from '#lib/api/graph.api.ts';
	import { DATASET_PAGE_NAMES } from '#lib/dataset-nav.ts';
	import { failureText } from '#lib/failure.ts';
	import { useColorMode } from '@workbench/ui/color-mode';
	import GpuGraph from '#lib/graph/gpu-graph.svelte';
	import { themeBackground } from '#lib/gpu/css-color.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import GraphBreadcrumb from '#lib/graph/graph-breadcrumb.svelte';
	import { gpuUnsupportedReason } from '#lib/gpu/gpu-support.ts';
	import { titleOf } from '#lib/nav.ts';

	let { data } = $props();
	const name = DATASET_PAGE_NAMES.graph;
	const dataset = $derived(data.id);

	type View = 'graph' | 'table' | 'json';
	type SimNode = SimulationNodeDatum & { id: string; idx: number };

	// ── type → colour, per theme: Tailwind 400 on the dark canvas, 600 on the light one, so a node
	// stands out from the canvas the theme clears it to. An entity in more than one document is drawn
	// in a more intense shade: towards white on dark, towards black on light.
	type Rgb = [number, number, number];
	const TYPE_RGB_DARK: Record<string, Rgb> = {
		PERSON: [56, 189, 248],
		ORG: [52, 211, 153],
		GEO: [251, 191, 36],
		EVENT: [167, 139, 250],
		CONCEPT: [244, 114, 182],
		WORK: [251, 146, 60],
		OTHER: [148, 163, 184],
	};
	const TYPE_RGB_LIGHT: Record<string, Rgb> = {
		PERSON: [2, 132, 199],
		ORG: [5, 150, 105],
		GEO: [217, 119, 6],
		EVENT: [124, 58, 237],
		CONCEPT: [219, 39, 119],
		WORK: [234, 88, 12],
		OTHER: [100, 116, 139],
	};
	const theme = useColorMode();
	const typeRgb = $derived(theme.isDark ? TYPE_RGB_DARK : TYPE_RGB_LIGHT);
	/** The canvas clears to the page's own `--background`, read again when the theme changes. */
	const canvasBackground = $derived((void theme.isDark, themeBackground()));
	function nodeColor(type: string, videos: number): Rgb {
		const base = typeRgb[type] ?? typeRgb.OTHER!;
		if (videos <= 1) return base;
		const toward = theme.isDark ? 255 : 0;
		return [
			Math.round(base[0] + (toward - base[0]) * 0.35),
			Math.round(base[1] + (toward - base[1]) * 0.35),
			Math.round(base[2] + (toward - base[2]) * 0.35),
		];
	}
	const sizeOf = (mentions: number): number =>
		8 + Math.min(20, Math.sqrt(Math.max(1, mentions)) * 4);

	const PRESETS: SelectOption[] = [
		{
			value: 'MATCH (a:Entity)-[:MENTIONS]->(c:Chunk)\nRETURN a.name, c.doc_id, c.start_s LIMIT 25',
			label: 'Entity → its clips',
		},
		{
			value:
				'MATCH (a:Entity)-[:MENTIONS]->(c:Chunk)<-[:MENTIONS]-(b:Entity)\nWHERE a.entity_id < b.entity_id\nRETURN a.name, b.name, count(c.chunk_id) AS shared ORDER BY shared DESC LIMIT 15',
			label: 'Co-occurrence (shared clips)',
		},
		{
			value: 'MATCH (a:Entity)-[:RELATIONSHIP]->(b:Entity)\nRETURN a.name, b.name LIMIT 25',
			label: 'Relationship neighbourhood',
		},
		{
			value:
				'MATCH (a:Entity)\nRETURN a.name, a.entity_type, a.mention_count ORDER BY a.mention_count DESC LIMIT 20',
			label: 'Top entities by mentions',
		},
		{
			value:
				'MATCH (a:Entity)\nRETURN a.entity_type, count(a.entity_id) AS n ORDER BY n DESC LIMIT 10',
			label: 'Entity-type breakdown',
		},
	];

	/** A plain question, what it shows, and its Cypher; one click fills the console and runs it. */
	const HELP_EXAMPLES = [
		{
			label: 'Who are the people?',
			desc: 'Named individuals, ranked by how many clips mention them.',
			query:
				"MATCH (a:Entity) WHERE a.entity_type = 'PERSON' RETURN a.name, a.mention_count ORDER BY a.mention_count DESC LIMIT 20",
		},
		{
			label: 'What are the topics?',
			desc: 'The most-discussed concepts across the dataset.',
			query:
				"MATCH (a:Entity) WHERE a.entity_type = 'CONCEPT' RETURN a.name, a.mention_count ORDER BY a.mention_count DESC LIMIT 20",
		},
		{
			label: 'Central entities',
			desc: 'Entities with the most connections.',
			query:
				'MATCH (a:Entity)-[:RELATIONSHIP]->(b:Entity) RETURN a.name, a.entity_type, count(b.entity_id) AS connections ORDER BY connections DESC LIMIT 15',
		},
		{
			label: 'Strongest links',
			desc: 'Relationships asserted most often, with their description.',
			query:
				'MATCH (a:Entity)-[r:RELATIONSHIP]->(b:Entity) RETURN a.name, b.name, r.weight, r.description ORDER BY r.weight DESC LIMIT 15',
		},
		{
			label: 'Search by word',
			desc: 'Entities whose name contains a word: edit the word in the query.',
			query:
				"MATCH (a:Entity) WHERE a.name_lower CONTAINS 'word' RETURN a.name, a.entity_type, a.mention_count LIMIT 20",
		},
	];

	// ── page state ──────────────────────────────────────────────────────────
	const status = $derived(graphStatus(dataset));
	let view = $state<View>('graph');
	let loading = $state(false);
	/** Why the last subgraph or entity could not be read. */
	let failure = $state<{ what: string; detail: string } | null>(null);
	let showHelp = $state(false);

	let searchQ = $state('');
	let matches = $state.raw<GraphMatch[]>([]);
	let dropdownOpen = $state(false);

	let presetValue = $state('');
	let cypherText = $state('');
	let cypherOpen = $state(false);
	let cypherResult = $state.raw<GraphCypherResponse | null>(null);
	let cypherRunning = $state(false);

	let detail = $state.raw<GraphEntityResponse | null>(null);
	let selectedId = $state<string | null>(null);
	/** The trail the breadcrumb shows; empty is the overview. */
	let trail = $state<{ id: string; name: string }[]>([]);

	// ── force-layout buffers (mutated in place; layoutVersion is the GPU signal) ──
	let graphNodes = $state.raw<GraphNode[]>([]);
	let idToIdx = $state.raw<Map<string, number>>(new Map());
	let nodesX = $state.raw(new Float32Array(0));
	let nodesY = $state.raw(new Float32Array(0));
	// Recoloured, not re-laid-out, when the theme changes.
	const nodeRgb = $derived.by(() => {
		const rgb = new Uint8Array(graphNodes.length * 3);
		graphNodes.forEach((node, i) => rgb.set(nodeColor(node.type, node.videos), i * 3));
		return rgb;
	});
	let nodeSize = $state.raw(new Float32Array(0));
	let nodeState = $state.raw(new Uint8Array(0));
	let edgesIdx = $state.raw(new Uint32Array(0));
	let layoutVersion = $state(0);
	let fitBounds = $state.raw({ minX: -1, minY: -1, maxX: 1, maxY: 1 });

	let sim: Simulation<SimNode, undefined> | null = null;
	let simNodes: SimNode[] = [];

	let gw = $state(0);
	let gh = $state(0);
	let hover = $state<{ name: string; type: string; x: number; y: number } | null>(null);

	const markerIndex = $derived(selectedId !== null ? (idToIdx.get(selectedId) ?? null) : null);

	// Whether this browser can draw the graph view: probed once. `undefined` is not yet known.
	let gpuReason = $state<string | null | undefined>(undefined);
	void gpuUnsupportedReason().then((reason) => (gpuReason = reason));

	function stopSim(): void {
		sim?.stop();
		sim = null;
	}

	function recomputeFit(): void {
		const n = nodesX.length;
		if (n === 0) return;
		let minX = Infinity;
		let minY = Infinity;
		let maxX = -Infinity;
		let maxY = -Infinity;
		for (let i = 0; i < n; i++) {
			minX = Math.min(minX, nodesX[i]!);
			minY = Math.min(minY, nodesY[i]!);
			maxX = Math.max(maxX, nodesX[i]!);
			maxY = Math.max(maxY, nodesY[i]!);
		}
		const padX = (maxX - minX) * 0.08 || 1;
		const padY = (maxY - minY) * 0.08 || 1;
		fitBounds = { minX: minX - padX, minY: minY - padY, maxX: maxX + padX, maxY: maxY + padY };
	}

	function buildLayout(nodes: GraphNode[], edges: { source: string; target: string }[]): void {
		stopSim();
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

		graphNodes = nodes;
		idToIdx = idx;
		nodesX = x;
		nodesY = y;
		nodeSize = size;
		nodeState = new Uint8Array(n);
		edgesIdx = ei;

		// The force layout settles synchronously (milliseconds at these node counts), so the nodes mount
		// in their final places and the camera fits once, not three times while a live simulation moves.
		simNodes = nodes.map((nd, i) => ({ id: nd.id, idx: i, x: x[i], y: y[i] }));
		const links = valid.map((e) => ({ source: e.source, target: e.target }));
		sim = forceSimulation<SimNode>(simNodes)
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
				forceCollide<SimNode>((d) => sizeOf(graphNodes[d.idx]?.mentions ?? 1) / 2 + 5),
			)
			.force('x', forceX<SimNode>(0).strength(0.04))
			.force('y', forceY<SimNode>(0).strength(0.04));
		for (let tick = 0; sim.alpha() > 0.025 && tick < 400; tick++) sim.tick();
		for (let i = 0; i < simNodes.length; i++) {
			nodesX[i] = simNodes[i]!.x ?? 0;
			nodesY[i] = simNodes[i]!.y ?? 0;
		}
		recomputeFit();
		layoutVersion++;
	}

	// ── reads ────────────────────────────────────────────────────────────────
	// Only the latest load writes the page: an overview or an entity answered after the reader asked for
	// another is dropped, never drawn over the newer one.
	let loadSeq = 0;
	let cypherSeq = 0;
	/** The load the reader last asked for, which "Try again" repeats. */
	let lastLoad: () => Promise<void> = () => loadOverview();

	async function loadOverview(): Promise<void> {
		const mine = ++loadSeq;
		lastLoad = loadOverview;
		loading = true;
		failure = null;
		try {
			const sub = await graphSubgraph({ dataset, entityId: null, limit: 120 });
			if (mine !== loadSeq) return;
			selectedId = null;
			detail = null;
			trail = [];
			buildLayout(sub.nodes, sub.edges);
		} catch (err) {
			if (mine === loadSeq)
				failure = { what: 'The graph could not be loaded.', detail: failureText(err) };
		} finally {
			if (mine === loadSeq) loading = false;
		}
	}

	async function selectEntity(id: string): Promise<void> {
		const mine = ++loadSeq;
		lastLoad = () => selectEntity(id);
		dropdownOpen = false;
		view = 'graph';
		loading = true;
		failure = null;
		try {
			const [sub, d] = await Promise.all([
				graphSubgraph({ dataset, entityId: id, limit: 60 }),
				graphEntity({ dataset, entityId: id }),
			]);
			if (mine !== loadSeq) return;
			selectedId = id;
			detail = d;
			// Revisiting a crumb truncates the trail back to it; a new hop appends.
			const at = trail.findIndex((c) => c.id === id);
			const label = d.entity?.name ?? id;
			trail = at >= 0 ? trail.slice(0, at + 1) : [...trail, { id, name: label }];
			buildLayout(sub.nodes, sub.edges);
		} catch (err) {
			if (mine === loadSeq) {
				failure = { what: 'This entity could not be loaded.', detail: failureText(err) };
			}
		} finally {
			if (mine === loadSeq) loading = false;
		}
	}

	async function runQuery(): Promise<void> {
		const q = cypherText.trim();
		if (!q) return;
		cypherOpen = true;
		view = 'table';
		cypherRunning = true;
		const mine = ++cypherSeq;
		const res = await runCypher(dataset, q);
		if (mine !== cypherSeq) return;
		cypherRunning = false;
		// A refused or unreachable console call shows in the table as the result's own error.
		cypherResult = res.ok ? res.data : { built: false, columns: [], rows: [], error: res.detail };
	}

	function runExample(query: string): void {
		cypherText = query;
		cypherOpen = true;
		void runQuery();
	}

	function onPreset(value: string): void {
		if (!value) return;
		cypherText = value;
		cypherOpen = true;
		presetValue = '';
		void runQuery();
	}

	// The entity search: a match list for two letters or more, a quarter second after the last key.
	let searchTimer: ReturnType<typeof setTimeout> | undefined;
	function onSearchInput(): void {
		const q = searchQ.trim();
		clearTimeout(searchTimer);
		if (q.length < 2) {
			matches = [];
			dropdownOpen = false;
			return;
		}
		searchTimer = setTimeout(() => {
			graphMatches({ dataset, q }).then(
				(r) => {
					if (searchQ.trim() !== q) return;
					matches = r.matches;
					dropdownOpen = true;
				},
				() => {
					matches = [];
					dropdownOpen = false;
				},
			);
		}, 250);
	}

	/** Close the match list when focus leaves the search and its list, not when it moves into the list. */
	function onSearchFocusOut(event: FocusEvent): void {
		const next = event.relatedTarget;
		if (next instanceof Node && (event.currentTarget as HTMLElement).contains(next)) return;
		dropdownOpen = false;
	}

	function onClip(docId: string, start: number): void {
		const params = new URLSearchParams(page.url.search);
		params.set('doc', docId);
		params.set('t', String(Math.max(0, Math.floor(start))));
		// The route id is resolved; the query the rule cannot see follows it.
		// oxlint-disable-next-line svelte/no-navigation-without-resolve
		void goto(`${resolve('/datasets/[id]', { id: encodeURIComponent(dataset) })}?${params}`);
	}

	function onPick(index: number | null): void {
		if (index === null) return;
		const node = graphNodes[index];
		if (node) void selectEntity(node.id);
	}

	function onHover(index: number | null, clientX: number, clientY: number): void {
		if (index === null) {
			hover = null;
			return;
		}
		const node = graphNodes[index];
		hover = node ? { name: node.name, type: node.type, x: clientX, y: clientY } : null;
	}

	const fmtTime = (s: number): string => {
		const m = Math.floor(s / 60);
		return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
	};

	// The overview loads once the status says the graph is built, again for another dataset.
	$effect(() => {
		const built = status.current?.built;
		if (built) untrack(() => void loadOverview());
		return stopSim;
	});
</script>

<svelte:head><title>{titleOf(name, dataset, 'Datasets')}</title></svelte:head>

<h1 class="sr-only">{name}</h1>

<div class="@container relative flex min-h-0 flex-1">
	<!-- ── main column: query bar + result views ───────────────────────────── -->
	<div class="flex min-w-0 flex-1 flex-col">
		<div class="border-border bg-card/40 flex flex-col gap-2 border-b p-3">
			<div class="flex flex-wrap items-center gap-2">
				<div class="relative w-full @2xl:w-64" onfocusout={onSearchFocusOut}>
					<Input
						class="h-8 rounded-lg"
						type="search"
						placeholder="Search entities…"
						aria-label="Search entities"
						aria-controls="graph-matches"
						aria-expanded={dropdownOpen}
						bind:value={searchQ}
						oninput={onSearchInput}
						onfocus={() => (dropdownOpen = matches.length > 0)}
						onkeydown={(e) => {
							if (e.key === 'Escape') dropdownOpen = false;
						}}
					/>
					{#if dropdownOpen}
						<ul
							id="graph-matches"
							aria-label="Matching entities"
							class="border-border bg-popover text-popover-foreground absolute z-20 mt-1 max-h-72 w-full overflow-auto rounded-lg border p-1 shadow-md @2xl:w-80"
						>
							{#each matches as m (m.entity_id)}
								<li>
									<button
										type="button"
										class="hover:bg-muted focus-visible:bg-muted flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm outline-none"
										onclick={() => selectEntity(m.entity_id)}
									>
										<span class="truncate">{m.name}</span>
										<span class="text-muted-foreground shrink-0 text-xs"
											>{m.entity_type} · {m.mention_count}×</span
										>
									</button>
								</li>
							{:else}
								<li class="text-muted-foreground px-2 py-1.5 text-sm">No entity matches.</li>
							{/each}
						</ul>
					{/if}
				</div>

				<Select
					options={PRESETS}
					bind:value={presetValue}
					onValueChange={onPreset}
					placeholder="Cypher presets…"
					ariaLabel="Cypher presets"
				/>

				<Button
					variant="outline"
					size="sm"
					aria-expanded={cypherOpen}
					onclick={() => (cypherOpen = !cypherOpen)}
				>
					{cypherOpen ? 'Hide Cypher' : 'Cypher'}
				</Button>
				<Button variant="outline" size="sm" disabled={!status.current?.built} onclick={loadOverview}
					>Overview</Button
				>

				<div class="ml-auto flex items-center gap-2">
					<Button
						variant={showHelp ? 'secondary' : 'ghost'}
						size="icon-sm"
						aria-pressed={showHelp}
						onclick={() => (showHelp = !showHelp)}
						title="How to read this"
						aria-label="How to read this"
					>
						<HelpCircle />
					</Button>
					<div
						class="border-border bg-background flex items-center gap-0.5 rounded-lg border p-0.5"
						role="group"
						aria-label="Result view"
					>
						{#each ['graph', 'table', 'json'] as const as v (v)}
							<Button
								variant={view === v ? 'secondary' : 'ghost'}
								size="xs"
								aria-pressed={view === v}
								onclick={() => (view = v)}
								class={v === 'json' ? '' : 'capitalize'}>{v === 'json' ? 'JSON' : v}</Button
							>
						{/each}
					</div>
				</div>
			</div>

			<GraphBreadcrumb {trail} onNavigate={(id) => (id ? selectEntity(id) : loadOverview())} />

			{#if cypherOpen}
				<div class="flex items-start gap-2">
					<textarea
						aria-label="Cypher query"
						class="border-input dark:bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 h-20 min-w-0 flex-1 rounded-lg border bg-transparent p-2 font-mono text-xs transition-colors outline-none focus-visible:ring-3"
						placeholder="MATCH (a:Entity)-[:RELATIONSHIP]->(b:Entity) RETURN a.name, b.name LIMIT 25"
						bind:value={cypherText}></textarea>
					<Button size="sm" disabled={cypherRunning} onclick={runQuery}>
						{cypherRunning ? 'Running…' : 'Run'}
					</Button>
				</div>
			{/if}

			{#if showHelp}
				<div
					class="bg-muted/40 border-border text-muted-foreground max-h-64 overflow-auto rounded-lg border p-3 text-xs leading-relaxed"
				>
					<p class="text-foreground mb-1 font-medium">What is this?</p>
					The dataset's knowledge graph. Each<b>node is an entity</b> (a person, an organisation, a
					place, an event…) and each <b>edge is a relationship</b> extracted from the dataset's
					text. It is queried live with Cypher.
					<p class="text-foreground mt-2 mb-1 font-medium">How to use it</p>
					<ul class="ml-4 list-disc space-y-0.5">
						<li><b>Search</b> an entity, or click a node: its panel opens.</li>
						<li>
							The panel lists the <b>clips</b> that mention it (open one to play it), and its
							<b>related</b> and <b>co-occurring</b> entities (pick one to re-centre).
						</li>
						<li>
							<b>Cypher presets</b> and the console answer precise questions, in the <b>Table</b>
							and
							<b>JSON</b> views.
						</li>
					</ul>
					<p class="text-foreground mt-2 mb-1 font-medium">How to read the graph</p>
					<ul class="ml-4 list-disc space-y-0.5">
						<li>
							<b>Node size</b> is how many clips mention it; a <b>more intense shade</b> means it appears
							in more than one document.
						</li>
						<li>
							<b>Colour</b> is the type (the legend, bottom left). Drag to pan, scroll to zoom, “Fit”
							to recentre.
						</li>
						<li>
							<b>One name can be several entities</b>: entities are linked by name only, so read
							each clip's context to tell them apart.
						</li>
					</ul>
				</div>
			{/if}
		</div>

		<!-- result area + example-query rail -->
		<div class="flex min-h-0 flex-1 flex-col @2xl:flex-row">
			<div class="relative min-h-0 flex-1">
				{#if status.error && !status.current}
					<div class="grid h-full place-items-center">
						<LoadFailure
							what="The knowledge graph could not be loaded."
							detail={failureText(status.error)}
							onretry={() => void status.refresh()}
						/>
					</div>
				{:else if !status.current}
					<div class="text-muted-foreground grid h-full place-items-center text-sm" role="status">
						Reading the graph…
					</div>
				{:else if !status.current.built}
					<div class="grid h-full place-items-center p-6 text-center">
						<div class="max-w-sm">
							<p class="text-foreground text-sm font-medium">No knowledge graph yet</p>
							<p class="text-muted-foreground mt-1 text-xs">
								This dataset declares a knowledge graph, but its tables hold no entities yet.
							</p>
						</div>
					</div>
				{:else if failure}
					<div class="grid h-full place-items-center">
						<LoadFailure
							what={failure.what}
							detail={failure.detail}
							onretry={() => void lastLoad()}
						/>
					</div>
				{:else if view === 'graph'}
					<div class="absolute inset-0" bind:clientWidth={gw} bind:clientHeight={gh}>
						{#if gpuReason}
							<div
								class="flex h-full flex-col items-center justify-center gap-1 px-6 text-center text-sm"
								role="status"
							>
								<span class="text-destructive">{gpuReason}</span>
								<span class="text-muted-foreground"
									>The graph view draws with WebGPU. The table, JSON and each entity's panel work
									without it.</span
								>
							</div>
						{:else if gw > 0 && gh > 0 && graphNodes.length > 0 && gpuReason === null}
							<GpuGraph
								{nodesX}
								{nodesY}
								{layoutVersion}
								{nodeRgb}
								{nodeSize}
								{nodeState}
								edges={edgesIdx}
								{fitBounds}
								width={gw}
								height={gh}
								background={canvasBackground}
								{markerIndex}
								{onHover}
								{onPick}
							/>
						{:else}
							<div
								class="text-muted-foreground grid h-full place-items-center text-sm"
								role="status"
							>
								{loading || gpuReason === undefined ? 'Reading the graph…' : 'No entities.'}
							</div>
						{/if}
						{#if hover}
							<div
								class="border-border bg-popover text-popover-foreground pointer-events-none fixed z-30 rounded-lg border px-2 py-1 text-xs shadow-md"
								style:left="{hover.x + 12}px"
								style:top="{hover.y + 12}px"
							>
								<span class="font-medium">{hover.name}</span>
								<span class="text-muted-foreground"> · {hover.type}</span>
							</div>
						{/if}
						{#if gpuReason === null && graphNodes.length > 0}
							<div class="absolute right-2 bottom-2">
								<Button variant="outline" size="sm" onclick={recomputeFit}>Fit</Button>
							</div>
							<div
								class="bg-card/85 border-border text-muted-foreground absolute bottom-2 left-2 flex max-w-[calc(100%-4.5rem)] flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border px-2 py-1 text-[0.7rem] backdrop-blur"
							>
								{#each Object.entries(typeRgb) as [t, rgb] (t)}
									<span class="flex items-center gap-1">
										<span
											class="size-2 rounded-full"
											style:background="rgb({rgb[0]},{rgb[1]},{rgb[2]})"
										></span>
										{t}
									</span>
								{/each}
								<span class="hidden @2xl:inline"
									>· size = mentions · click a node for its clips</span
								>
							</div>
						{/if}
					</div>
				{:else if view === 'table'}
					<div class="h-full overflow-auto p-3">
						{#if cypherResult?.error}
							<pre
								class="text-destructive border-destructive/40 bg-destructive/10 rounded-lg border p-3 text-xs whitespace-pre-wrap"
								role="alert">{cypherResult.error}</pre>
						{:else if cypherResult && cypherResult.columns.length > 0}
							<table class="w-full text-left text-sm">
								<thead class="text-muted-foreground border-border border-b">
									<tr>
										{#each cypherResult.columns as c (c)}<th class="px-2 py-1 font-medium">{c}</th
											>{/each}
									</tr>
								</thead>
								<tbody>
									{#each cypherResult.rows as row, i (i)}
										<tr class="border-border/50 border-b">
											{#each row as cell, j (j)}
												<td class="px-2 py-1 align-top break-words">{cell === null ? '—' : cell}</td
												>
											{/each}
										</tr>
									{/each}
								</tbody>
							</table>
							<p class="text-muted-foreground mt-2 text-xs">{cypherResult.rows.length} rows</p>
						{:else if cypherResult}
							<p class="text-muted-foreground text-sm">The query answered no rows.</p>
						{:else}
							<p class="text-muted-foreground text-sm">Run a Cypher query to see rows here.</p>
						{/if}
					</div>
				{:else}
					<pre class="h-full overflow-auto p-3 text-xs">{cypherResult
							? JSON.stringify(cypherResult.rows, null, 2)
							: 'Run a Cypher query to see JSON here.'}</pre>
				{/if}
			</div>

			{#if cypherOpen || view !== 'graph'}
				<aside
					aria-label="Example questions"
					class="border-border bg-card/20 max-h-56 w-full shrink-0 space-y-2 overflow-auto border-t p-3 @2xl:max-h-none @2xl:w-80 @2xl:border-t-0 @2xl:border-l"
				>
					<div>
						<p class="text-foreground text-sm font-semibold">Example questions</p>
						<p class="text-muted-foreground text-xs">Pick one to run it.</p>
					</div>
					{#each HELP_EXAMPLES as ex (ex.label)}
						<button
							type="button"
							class="border-border bg-background hover:border-primary/50 hover:bg-muted/60 focus-visible:ring-ring/50 block w-full space-y-1 rounded-lg border p-2.5 text-left transition-colors outline-none focus-visible:ring-3"
							onclick={() => runExample(ex.query)}
						>
							<span class="text-foreground block text-sm font-medium">{ex.label}</span>
							<span class="text-muted-foreground block text-xs leading-snug">{ex.desc}</span>
							<code
								class="text-muted-foreground bg-muted/50 mt-1 block truncate rounded-md px-1.5 py-0.5 font-mono text-[0.7rem]"
								>{ex.query}</code
							>
						</button>
					{/each}
				</aside>
			{/if}
		</div>
	</div>

	<!-- ── entity panel: beside the results when wide, over them when narrow ───── -->
	{#if detail?.entity}
		{@const e = detail.entity}
		<aside
			aria-labelledby="graph-entity-heading"
			class="border-border bg-card absolute inset-0 z-20 w-full overflow-auto p-3 @2xl:static @2xl:w-80 @2xl:shrink-0 @2xl:border-l"
		>
			<div class="flex items-start justify-between gap-2">
				<h2 id="graph-entity-heading" class="text-foreground text-sm font-semibold break-words">
					{e.name}
				</h2>
				<Button
					variant="ghost"
					size="icon-xs"
					aria-label="Close {e.name}"
					title="Close"
					onclick={() => (detail = null)}
				>
					<X />
				</Button>
			</div>
			<div class="text-muted-foreground mt-1 flex flex-wrap gap-1 text-xs">
				<span class="bg-secondary rounded-full px-2 py-0.5">{e.entity_type}</span>
				<span class="bg-secondary rounded-full px-2 py-0.5">{e.mention_count} mentions</span>
			</div>

			{#if detail.clips.length > 0}
				<h3 class="text-muted-foreground mt-4 mb-1 text-xs font-medium tracking-wide uppercase">
					Clips ({detail.clips.length})
				</h3>
				<ul class="flex flex-col gap-1">
					{#each detail.clips as c (c.chunk_id)}
						<li>
							<button
								type="button"
								class="hover:bg-muted/60 focus-visible:ring-ring/50 w-full rounded-md p-1.5 text-left outline-none focus-visible:ring-3"
								onclick={() => onClip(c.doc_id, c.start)}
								title="Open in the player"
							>
								<span class="text-foreground flex items-center gap-1 text-xs">
									<span class="text-primary font-mono">{fmtTime(c.start)}</span>
									<span class="text-muted-foreground truncate">{c.title}</span>
								</span>
								{#if c.text}<span class="text-muted-foreground mt-0.5 line-clamp-2 block text-xs">
										{c.text}
									</span>{/if}
							</button>
						</li>
					{/each}
				</ul>
			{/if}

			{#if detail.neighbors.length > 0}
				<h3 class="text-muted-foreground mt-4 mb-1 text-xs font-medium tracking-wide uppercase">
					Related
				</h3>
				<div class="flex flex-wrap gap-1">
					{#each detail.neighbors as nb (nb.entity_id + nb.direction)}
						<button
							type="button"
							class="border-border bg-secondary text-secondary-foreground hover:bg-muted/60 focus-visible:ring-ring/50 rounded-full border px-2 py-0.5 text-xs transition-colors outline-none focus-visible:ring-3"
							title={nb.description || nb.name}
							onclick={() => selectEntity(nb.entity_id)}
						>
							<span aria-hidden="true">{nb.direction === 'out' ? '→ ' : '← '}</span><span
								class="sr-only">{nb.direction === 'out' ? 'to ' : 'from '}</span
							>{nb.name}
						</button>
					{/each}
				</div>
			{/if}

			{#if detail.cooccur.length > 0}
				<h3 class="text-muted-foreground mt-4 mb-1 text-xs font-medium tracking-wide uppercase">
					Co-occurs with
				</h3>
				<div class="flex flex-wrap gap-1">
					{#each detail.cooccur as co (co.entity_id)}
						<button
							type="button"
							class="border-border bg-secondary text-secondary-foreground hover:bg-muted/60 focus-visible:ring-ring/50 rounded-full border px-2 py-0.5 text-xs transition-colors outline-none focus-visible:ring-3"
							onclick={() => selectEntity(co.entity_id)}
						>
							{co.name} <span class="text-muted-foreground">·{co.shared}</span>
						</button>
					{/each}
				</div>
			{/if}
		</aside>
	{/if}
</div>
