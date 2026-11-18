<script lang="ts">
	/**
	 * The dataset's knowledge graph as a dock panel: an entity search, Cypher presets and a console drive a
	 * result area with three views, the graph (a d3-force layout drawn by the WebGPU `<GpuGraph>`), a table
	 * and JSON. Picking a node opens its panel: properties, the clips that mention it (each opens in the
	 * Player panel at that moment) and the related and co-occurring entities, which re-centre the graph.
	 * Picking an entity also lists the rows that name it in the Results panel, which the Atlas lights up
	 * (`#lib/graph/graph-explorer.svelte.ts`, which holds what must survive the dock remounting the panel).
	 *
	 * The graph view needs WebGPU; the table and JSON views and the entity panel do not, so a browser
	 * without it still reads the graph. Narrow (the panel's own width, a container query), the side panels
	 * cover the result area instead of squeezing it, and the example queries stack under it.
	 */
	import { untrack } from 'svelte';
	import { HelpCircle, X } from '@lucide/svelte';
	import { Button } from '@workbench/ui';
	import { Input } from '@workbench/ui/input';
	import { Select, type SelectOption } from '@workbench/ui/select';
	import { appearance } from '@workbench/ui/appearance';
	import { graphStatus } from '#lib/api/graph.api.ts';
	import { failureText } from '#lib/failure.ts';
	import GpuGraph from '#lib/graph/gpu-graph.svelte';
	import GraphBreadcrumb from '#lib/graph/graph-breadcrumb.svelte';
	import { graphExplorer as g } from '#lib/graph/graph-explorer.svelte.ts';
	import { themeBackground } from '#lib/gpu/css-color.ts';
	import { gpuUnsupportedReason } from '#lib/gpu/gpu-support.ts';
	import { activeView } from '@workbench/explorer-api/descriptor';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import SearchRowsPanel from '#lib/explore/components/search-rows-panel.svelte';
	import { dockControl } from '#lib/dock/dock-control.svelte.ts';

	// The dataset the graph's state is for (`graphExplorer.open`), not one a navigation is still loading.
	const dataset = $derived(g.dataset ?? '');
	const status = $derived(graphStatus(dataset));

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
	const typeRgb = $derived(appearance.dark ? TYPE_RGB_DARK : TYPE_RGB_LIGHT);
	/** The canvas clears to the page's own `--background`, read again when the theme changes. */
	const canvasBackground = $derived((void appearance.resolved, themeBackground()));
	function nodeColor(type: string, videos: number): Rgb {
		const base = typeRgb[type] ?? typeRgb.OTHER!;
		if (videos <= 1) return base;
		const toward = appearance.dark ? 255 : 0;
		return [
			Math.round(base[0] + (toward - base[0]) * 0.35),
			Math.round(base[1] + (toward - base[1]) * 0.35),
			Math.round(base[2] + (toward - base[2]) * 0.35),
		];
	}

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

	let presetValue = $state('');

	function onPreset(value: string): void {
		if (!value) return;
		g.cypherText = value;
		g.cypherOpen = true;
		presetValue = '';
		void g.runQuery();
	}

	/** Maximized, the panel stands in for the page it replaced and lists the entity's rows itself. */
	const maximized = $derived(dockControl.maximized === 'graph');

	/** The option the arrows have moved to; -1 is none. */
	let highlighted = $state(-1);

	/** The combobox's keys: the arrows move through the matches, Enter picks one, Escape closes the list. */
	function onSearchKey(event: KeyboardEvent): void {
		const count = g.matches.length;
		if (event.key === 'Escape') {
			if (g.dropdownOpen) event.preventDefault();
			g.dropdownOpen = false;
			highlighted = -1;
		} else if (event.key === 'ArrowDown' && count > 0) {
			event.preventDefault();
			g.dropdownOpen = true;
			highlighted = (highlighted + 1) % count;
		} else if (event.key === 'ArrowUp' && count > 0) {
			event.preventDefault();
			g.dropdownOpen = true;
			highlighted = highlighted <= 0 ? count - 1 : highlighted - 1;
		} else if (event.key === 'Enter' && g.dropdownOpen && highlighted >= 0) {
			event.preventDefault();
			const match = g.matches[highlighted];
			highlighted = -1;
			if (match) void g.selectEntity(match.entity_id);
		}
	}

	/** Close the match list when focus leaves the search and its list, not when it moves into the list. */
	function onSearchFocusOut(event: FocusEvent): void {
		const next = event.relatedTarget;
		if (next instanceof Node && (event.currentTarget as HTMLElement).contains(next)) return;
		g.dropdownOpen = false;
	}

	// Recoloured, not re-laid-out, when the theme changes.
	const nodeRgb = $derived.by(() => {
		const rgb = new Uint8Array(g.nodes.length * 3);
		g.nodes.forEach((node, i) => rgb.set(nodeColor(node.type, node.videos), i * 3));
		return rgb;
	});
	const markerIndex = $derived(
		g.selectedId !== null ? (g.idToIdx.get(g.selectedId) ?? null) : null,
	);

	let gw = $state(0);
	let gh = $state(0);
	let hover = $state<{ name: string; type: string; x: number; y: number } | null>(null);

	// Whether this browser can draw the graph view: probed once. `undefined` is not yet known.
	let gpuReason = $state<string | null | undefined>(undefined);
	void gpuUnsupportedReason().then((reason) => (gpuReason = reason));

	function onPick(index: number | null): void {
		if (index === null) return;
		const node = g.nodes[index];
		if (node) void g.selectEntity(node.id);
	}

	function onHover(index: number | null, clientX: number, clientY: number): void {
		if (index === null) {
			hover = null;
			return;
		}
		const node = g.nodes[index];
		hover = node ? { name: node.name, type: node.type, x: clientX, y: clientY } : null;
	}

	/** Whether a clip has a moment to show: a page corpus's clips have none. */
	const timed = activeView().hasTime;

	const fmtTime = (s: number): string => {
		const m = Math.floor(s / 60);
		return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
	};

	// The subgraph is laid out once the status says the graph is built; a remount draws the one held.
	// Once per dataset (`ensureLoaded` is idempotent), and untracked: the load's own state must not ask for
	// another load, so a failed read waits for Try again.
	$effect(() => {
		if (status.current?.built) untrack(() => g.ensureLoaded());
	});
</script>

<section class="flex h-full min-h-0 flex-col overflow-auto" aria-labelledby="graph-heading">
	<h2 id="graph-heading" class="sr-only">Graph</h2>
	<div class="flex flex-1">
		<!-- The container a width query measures (the panel's own width, not the window's); the row inside it
	     lays the entity's detail below the graph when narrow and beside it when wide. -->
		<div class="@container flex min-w-0 flex-1">
			<div class="relative flex min-w-0 flex-1 flex-col @2xl:flex-row">
				<!-- ── main column: query bar + result views ───────────────────────────── -->
				<!-- The graph keeps a usable height however short the panel: below it, the panel scrolls. -->
				<div class="flex min-h-72 min-w-0 flex-1 flex-col">
					<div class="border-border bg-card/40 flex flex-col gap-2 border-b p-3">
						<div class="flex flex-wrap items-center gap-2">
							<div class="relative w-full @2xl:w-64" onfocusout={onSearchFocusOut}>
								<!-- A combobox over its listbox (WAI-ARIA APG): the arrows move the option in view, Enter picks
						     it, Escape closes the list; focus stays in the box throughout. -->
								<Input
									class="h-8 rounded-lg"
									type="search"
									role="combobox"
									placeholder="Search entities…"
									aria-label="Search entities"
									aria-autocomplete="list"
									aria-expanded={g.dropdownOpen}
									aria-controls={g.dropdownOpen ? 'graph-matches' : undefined}
									aria-activedescendant={g.dropdownOpen && highlighted >= 0
										? `graph-match-${highlighted}`
										: undefined}
									bind:value={g.searchQ}
									oninput={() => {
										highlighted = -1;
										g.searchInput();
									}}
									onfocus={() => (g.dropdownOpen = g.matches.length > 0)}
									onkeydown={onSearchKey}
								/>
								{#if g.dropdownOpen}
									<ul
										id="graph-matches"
										role="listbox"
										aria-label="Matching entities"
										class="border-border bg-popover text-popover-foreground absolute z-20 mt-1 max-h-72 w-full overflow-auto rounded-lg border p-1 shadow-md @2xl:w-80"
									>
										{#each g.matches as m, i (m.entity_id)}
											<!-- The pointer picks an option without taking focus from the box; the keyboard picks it from
									     the box itself (`onSearchKey`), as the combobox pattern has it. -->
											<!-- svelte-ignore a11y_click_events_have_key_events -->
											<li
												id="graph-match-{i}"
												role="option"
												aria-selected={i === highlighted}
												class={[
													'flex w-full cursor-pointer items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm',
													i === highlighted ? 'bg-muted' : 'hover:bg-muted',
												]}
												onpointerdown={(e) => e.preventDefault()}
												onclick={() => void g.selectEntity(m.entity_id)}
											>
												<span class="truncate">{m.name}</span>
												<span class="text-muted-foreground shrink-0 text-xs"
													>{m.entity_type} · {m.mention_count}×</span
												>
											</li>
										{:else}
											<li
												role="option"
												aria-disabled="true"
												aria-selected="false"
												class="text-muted-foreground px-2 py-1.5 text-sm"
											>
												No entity matches.
											</li>
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
								aria-expanded={g.cypherOpen}
								onclick={() => (g.cypherOpen = !g.cypherOpen)}
							>
								{g.cypherOpen ? 'Hide Cypher' : 'Cypher'}
							</Button>
							<Button
								variant="outline"
								size="sm"
								disabled={!status.current?.built}
								onclick={() => void g.loadOverview()}>Overview</Button
							>

							<div class="ml-auto flex items-center gap-2">
								<Button
									variant={g.showHelp ? 'secondary' : 'ghost'}
									size="icon-sm"
									aria-pressed={g.showHelp}
									onclick={() => (g.showHelp = !g.showHelp)}
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
											variant={g.view === v ? 'secondary' : 'ghost'}
											size="xs"
											aria-pressed={g.view === v}
											onclick={() => (g.view = v)}
											class={v === 'json' ? '' : 'capitalize'}>{v === 'json' ? 'JSON' : v}</Button
										>
									{/each}
								</div>
							</div>
						</div>

						<GraphBreadcrumb
							trail={g.trail}
							onNavigate={(id) => (id ? g.selectEntity(id) : g.loadOverview())}
						/>

						{#if g.cypherOpen}
							<div class="flex items-start gap-2">
								<textarea
									aria-label="Cypher query"
									class="border-input dark:bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 h-20 min-w-0 flex-1 rounded-lg border bg-transparent p-2 font-mono text-xs transition-colors outline-none focus-visible:ring-3"
									placeholder="MATCH (a:Entity)-[:RELATIONSHIP]->(b:Entity) RETURN a.name, b.name LIMIT 25"
									bind:value={g.cypherText}></textarea>
								<Button size="sm" disabled={g.cypherRunning} onclick={() => void g.runQuery()}>
									{g.cypherRunning ? 'Running…' : 'Run'}
								</Button>
							</div>
						{/if}

						{#if g.showHelp}
							<div
								class="bg-muted/40 border-border text-muted-foreground max-h-64 overflow-auto rounded-lg border p-3 text-xs leading-relaxed"
							>
								<p class="text-foreground mb-1 font-medium">What is this?</p>
								The dataset's knowledge graph. Each<b>node is an entity</b> (a person, an
								organisation, a place, an event…) and each <b>edge is a relationship</b> extracted
								from the dataset's text. It is queried live with Cypher.
								<p class="text-foreground mt-2 mb-1 font-medium">How to use it</p>
								<ul class="ml-4 list-disc space-y-0.5">
									<li><b>Search</b> an entity, or click a node: its panel opens.</li>
									<li>
										The panel lists the <b>clips</b> that mention it (open one to play it), and its
										<b>related</b> and <b>co-occurring</b> entities (pick one to re-centre).
									</li>
									<li>
										<b>Cypher presets</b> and the console answer precise questions, in the
										<b>Table</b>
										and
										<b>JSON</b> views.
									</li>
								</ul>
								<p class="text-foreground mt-2 mb-1 font-medium">How to read the graph</p>
								<ul class="ml-4 list-disc space-y-0.5">
									<li>
										<b>Node size</b> is how many clips mention it; a <b>more intense shade</b> means it
										appears in more than one document.
									</li>
									<li>
										<b>Colour</b> is the type (the legend, bottom left). Drag to pan, scroll to zoom,
										“Fit” to recentre.
									</li>
									<li>
										<b>One name can be several entities</b>: entities are linked by name only, so
										read each clip's context to tell them apart.
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
								<div
									class="text-muted-foreground grid h-full place-items-center text-sm"
									role="status"
								>
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
							{:else if g.failure}
								<div class="grid h-full place-items-center">
									<LoadFailure
										what={g.failure.what}
										detail={g.failure.detail}
										onretry={() => void g.retry()}
									/>
								</div>
							{:else if g.view === 'graph'}
								<div class="absolute inset-0" bind:clientWidth={gw} bind:clientHeight={gh}>
									{#if gpuReason}
										<div
											class="flex h-full flex-col items-center justify-center gap-1 px-6 text-center text-sm"
											role="status"
										>
											<span class="text-destructive">{gpuReason}</span>
											<span class="text-muted-foreground"
												>The graph view draws with WebGPU. The table, JSON and each entity's panel
												work without it.</span
											>
										</div>
									{:else if gw > 0 && gh > 0 && g.nodes.length > 0 && gpuReason === null}
										<GpuGraph
											nodesX={g.nodesX}
											nodesY={g.nodesY}
											layoutVersion={g.layoutVersion}
											{nodeRgb}
											nodeSize={g.nodeSize}
											nodeState={g.nodeState}
											edges={g.edgesIdx}
											fitBounds={g.fitBounds}
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
											{g.loading || gpuReason === undefined ? 'Reading the graph…' : 'No entities.'}
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
									{#if gpuReason === null && g.nodes.length > 0}
										<div class="absolute right-2 bottom-2">
											<Button variant="outline" size="sm" onclick={() => g.recomputeFit()}
												>Fit</Button
											>
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
							{:else if g.view === 'table'}
								<div class="h-full overflow-auto p-3">
									{#if g.cypherResult?.error}
										<pre
											class="text-destructive border-destructive/40 bg-destructive/10 rounded-lg border p-3 text-xs whitespace-pre-wrap"
											role="alert">{g.cypherResult.error}</pre>
									{:else if g.cypherResult && g.cypherResult.columns.length > 0}
										<table class="w-full text-left text-sm">
											<thead class="text-muted-foreground border-border border-b">
												<tr>
													{#each g.cypherResult.columns as c (c)}<th class="px-2 py-1 font-medium"
															>{c}</th
														>{/each}
												</tr>
											</thead>
											<tbody>
												{#each g.cypherResult.rows as row, i (i)}
													<tr class="border-border/50 border-b">
														{#each row as cell, j (j)}
															<td class="px-2 py-1 align-top break-words"
																>{cell === null ? '—' : cell}</td
															>
														{/each}
													</tr>
												{/each}
											</tbody>
										</table>
										<p class="text-muted-foreground mt-2 text-xs">
											{g.cypherResult.rows.length} rows
										</p>
									{:else if g.cypherResult}
										<p class="text-muted-foreground text-sm">The query answered no rows.</p>
									{:else}
										<p class="text-muted-foreground text-sm">
											Run a Cypher query to see rows here.
										</p>
									{/if}
								</div>
							{:else}
								<pre class="h-full overflow-auto p-3 text-xs">{g.cypherResult
										? JSON.stringify(g.cypherResult.rows, null, 2)
										: 'Run a Cypher query to see JSON here.'}</pre>
							{/if}
						</div>

						{#if g.cypherOpen || g.view !== 'graph'}
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
										onclick={() => g.runExample(ex.query)}
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

				<!-- ── entity panel: beside the graph when the panel is wide, below it when narrow, so the graph and
		     its marked node stay in view ───── -->
				{#if g.detailOpen && g.detail?.entity}
					{@const e = g.detail.entity}
					<aside
						aria-labelledby="graph-entity-heading"
						class="border-border bg-card max-h-[45%] w-full shrink-0 overflow-auto border-t p-3 @2xl:max-h-none @2xl:w-80 @2xl:border-t-0 @2xl:border-l"
					>
						<div class="flex items-start justify-between gap-2">
							<h2
								id="graph-entity-heading"
								class="text-foreground text-sm font-semibold break-words"
							>
								{e.name}
							</h2>
							<Button
								variant="ghost"
								size="icon-xs"
								aria-label="Close {e.name}"
								title="Close"
								onclick={() => (g.detailOpen = false)}
							>
								<X />
							</Button>
						</div>
						<div class="text-muted-foreground mt-1 flex flex-wrap gap-1 text-xs">
							<span class="bg-secondary rounded-full px-2 py-0.5">{e.entity_type}</span>
							<span class="bg-secondary rounded-full px-2 py-0.5"
								>{e.mention_count} {e.mention_count === 1 ? 'mention' : 'mentions'}</span
							>
						</div>

						{#if g.detail.clips.length > 0}
							<h3
								class="text-muted-foreground mt-4 mb-1 text-xs font-medium tracking-wide uppercase"
							>
								Clips ({g.detail.clips.length})
							</h3>
							<ul class="flex flex-col gap-1">
								{#each g.detail.clips as c (c.chunk_id)}
									<li>
										<button
											type="button"
											class="hover:bg-muted/60 focus-visible:ring-ring/50 w-full rounded-md p-1.5 text-left outline-none focus-visible:ring-3"
											onclick={() => g.playClip(c)}
											title="Open in the player"
										>
											<span class="text-foreground flex items-center gap-1 text-xs">
												{#if timed}<span class="text-primary font-mono">{fmtTime(c.start)}</span
													>{/if}
												<span class="text-muted-foreground truncate">{c.title}</span>
											</span>
											{#if c.text}<span
													class="text-muted-foreground mt-0.5 line-clamp-2 block text-xs"
												>
													{c.text}
												</span>{/if}
										</button>
									</li>
								{/each}
							</ul>
						{/if}

						{#if g.detail.neighbors.length > 0}
							<h3
								class="text-muted-foreground mt-4 mb-1 text-xs font-medium tracking-wide uppercase"
							>
								Related
							</h3>
							<div class="flex flex-wrap gap-1">
								{#each g.detail.neighbors as nb (nb.entity_id + nb.direction)}
									<button
										type="button"
										class="border-border bg-secondary text-secondary-foreground hover:bg-muted/60 focus-visible:ring-ring/50 rounded-full border px-2 py-0.5 text-xs transition-colors outline-none focus-visible:ring-3"
										title={nb.description || nb.name}
										onclick={() => g.selectEntity(nb.entity_id)}
									>
										<span aria-hidden="true">{nb.direction === 'out' ? '→ ' : '← '}</span><span
											class="sr-only">{nb.direction === 'out' ? 'to ' : 'from '}</span
										>{nb.name}
									</button>
								{/each}
							</div>
						{/if}

						{#if g.detail.cooccur.length > 0}
							<h3
								class="text-muted-foreground mt-4 mb-1 text-xs font-medium tracking-wide uppercase"
							>
								Co-occurs with
							</h3>
							<div class="flex flex-wrap gap-1">
								{#each g.detail.cooccur as co (co.entity_id)}
									<button
										type="button"
										class="border-border bg-secondary text-secondary-foreground hover:bg-muted/60 focus-visible:ring-ring/50 rounded-full border px-2 py-0.5 text-xs transition-colors outline-none focus-visible:ring-3"
										onclick={() => g.selectEntity(co.entity_id)}
									>
										{co.name} <span class="text-muted-foreground">·{co.shared}</span>
									</button>
								{/each}
							</div>
						{/if}
					</aside>
				{/if}
			</div>
		</div>
		{#if maximized && g.detailOpen && g.detail?.entity}
			<!-- Maximized, the graph stands in for its page: the rows naming the entity, playable in place. -->
			<div class="border-border w-96 max-w-[40%] shrink-0 border-l">
				<SearchRowsPanel
					title={g.detail.entity.name}
					empty="No rows name this entity."
					onClose={() => (g.detailOpen = false)}
				/>
			</div>
		{/if}
	</div>
</section>
