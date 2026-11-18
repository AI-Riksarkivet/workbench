<script lang="ts">
	/**
	 * The Graph of a table nobody stamped, from the columns a person picked (`#lib/plain/setup.ts`): an edge
	 * table's from and to, or a self-referencing table's id and the id each row points to. Until it is set up,
	 * the panel is its set-up form, which shows what a bounded read of the picked columns holds before it is
	 * drawn.
	 *
	 * The viewer answers the heaviest edges and the best-connected nodes in the shape the stamped graph lays
	 * out, drawn by the same d3-force layout and WebGPU renderer (`#lib/graph/force-layout.ts`,
	 * `#lib/graph/gpu-graph.svelte`). Picking a node opens its detail (its group, its degree, its
	 * neighbours, each of which re-centres the selection) and lists the rows that name it in the Rows panel,
	 * by the set-up's own columns (`plain.pickNode`). What the read left out is said above the graph.
	 */
	import { Loader2, Settings2, X } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';
	import { appearance } from '@workbench/ui/appearance';
	import { derivedGraph, tableProfile } from '#lib/api/derived.api.ts';
	import { buildHuePalette } from '#lib/atlas/atlas-colors.ts';
	import { dockControl } from '#lib/dock/dock-control.svelte.ts';
	import { failureText } from '#lib/failure.ts';
	import { themeBackground } from '#lib/gpu/css-color.ts';
	import { gpuUnsupportedReason } from '#lib/gpu/gpu-support.ts';
	import { fitOf, layoutGraph } from '#lib/graph/force-layout.ts';
	import GpuGraph from '#lib/graph/gpu-graph.svelte';
	import NoTable from '#lib/plain/NoTable.svelte';
	import SetupForm from '#lib/plain/SetupForm.svelte';
	import { extentNote } from '#lib/plain/extent.ts';
	import { plain } from '#lib/plain/plain.svelte.ts';
	import { missingColumns, type GraphSetup } from '#lib/plain/setup.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import RowsPanel from './RowsPanel.svelte';

	const facts = $derived(plain.facts);
	const table = $derived(plain.table);
	const info = $derived(facts && table ? (facts.tables[table] ?? null) : null);
	const setup = $derived(plain.setups.graph);
	const missing = $derived(
		setup && info
			? missingColumns(
					setup,
					info.columns.map((c) => c.name),
				)
			: [],
	);
	const args = $derived(
		facts && table && info ? { dataset: facts.id, table, version: info.version } : null,
	);
	const editing = $derived(plain.editing.graph || setup === undefined);
	const profile = $derived(args && editing ? tableProfile(args) : null);
	const read = $derived(
		args && setup && !editing && missing.length === 0 ? derivedGraph({ ...args, setup }) : null,
	);
	const graph = $derived(read?.current ?? null);
	const laid = $derived(graph ? layoutGraph(graph.nodes, graph.edges) : null);
	const fit = $derived(laid ? fitOf(laid.x, laid.y) : { minX: -1, minY: -1, maxX: 1, maxY: 1 });
	/** The renderer re-uploads positions when this changes: a new layout is a new number. */
	let layouts = 0;
	const layoutVersion = $derived((void laid, ++layouts));

	let gpuReason = $state<string | null | undefined>(undefined);
	void gpuUnsupportedReason().then((reason) => (gpuReason = reason));

	// The page's background, read again whenever the theme, mode or accent changes.
	const background = $derived((void appearance.resolved, themeBackground()));
	/** The groups, most common first, each with its hue. */
	const groups = $derived.by(() => {
		if (!graph) return [];
		const counts: Record<string, number> = {};
		for (const node of graph.nodes) counts[node.type] = (counts[node.type] ?? 0) + 1;
		const ranked = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
		const palette = buildHuePalette(Math.max(1, ranked.length), appearance.dark);
		return ranked.map(([name, count], i) => ({ name, count, rgb: palette[i]! }));
	});
	const nodeRgb = $derived.by(() => {
		if (!graph) return new Uint8Array(0);
		const hue = Object.fromEntries(groups.map((g) => [g.name, g.rgb]));
		const rgb = new Uint8Array(graph.nodes.length * 3);
		graph.nodes.forEach((node, i) => {
			const c = hue[node.type];
			if (c) rgb.set([c.r, c.g, c.b], i * 3);
		});
		return rgb;
	});
	const nodeState = $derived(new Uint8Array(graph?.nodes.length ?? 0));

	const chosen = $derived(plain.node?.of === 'graph' ? plain.node.key : null);
	const markerIndex = $derived(chosen !== null && laid ? (laid.idToIdx.get(chosen) ?? null) : null);
	const detail = $derived.by(() => {
		if (!graph || chosen === null) return null;
		const node = graph.nodes.find((nd) => nd.id === chosen);
		if (!node) return null;
		const name = (id: string): string => graph.nodes.find((nd) => nd.id === id)?.name ?? id;
		// One chip per neighbour and direction, with every relation the edges between them carry.
		const linked: Record<string, { id: string; name: string; out: boolean; relations: string[] }> =
			{};
		for (const e of graph.edges) {
			const out = e.source === chosen;
			if (!out && e.target !== chosen) continue;
			const other = out ? e.target : e.source;
			const key = `${out ? '>' : '<'}${other}`;
			linked[key] ??= { id: other, name: name(other), out, relations: [] };
			if (e.description && !linked[key].relations.includes(e.description)) {
				linked[key].relations.push(e.description);
			}
		}
		return { node, neighbours: Object.values(linked).slice(0, 60) };
	});

	let gw = $state(0);
	let gh = $state(0);
	let hover = $state<{ name: string; type: string; x: number; y: number } | null>(null);

	/** Maximized, the panel lists the node's rows itself, as a page of its own would. */
	const maximized = $derived(dockControl.maximized === 'graph');
	/** The panel's own width: the rows stand beside it only where both fit. */
	let panelWidth = $state(0);
	const beside = $derived(maximized && panelWidth >= 768);

	function pick(id: string, name: string): void {
		plain.pickNode('graph', id, name);
		if (dockControl.maximized !== 'graph') dockControl.reveal('results');
	}

	function onPick(index: number | null): void {
		const node = index === null ? undefined : graph?.nodes[index];
		if (node) pick(node.id, node.name);
	}

	function onHover(index: number | null, clientX: number, clientY: number): void {
		const node = index === null ? undefined : graph?.nodes[index];
		hover = node ? { name: node.name, type: node.type, x: clientX, y: clientY } : null;
	}

	const n = (v: number): string => v.toLocaleString();
</script>

<section
	class="flex h-full min-h-0 flex-col"
	aria-labelledby="plain-graph-heading"
	bind:clientWidth={panelWidth}
>
	<h2 id="plain-graph-heading" class="sr-only">Graph</h2>
	{#if !args}
		<NoTable />
	{:else if editing}
		<div class="min-h-0 flex-1 overflow-auto">
			{#if profile?.error && !profile.current}
				<div class="grid h-full place-items-center">
					<LoadFailure
						what="The table's columns could not be read."
						detail={failureText(profile.error)}
						onretry={() => void profile.refresh()}
					/>
				</div>
			{:else if !profile?.current}
				<p class="text-muted-foreground p-6 text-center text-sm" role="status">
					Reading a sample of the table to suggest columns…
				</p>
			{:else}
				<SetupForm
					panel="graph"
					dataset={args.dataset}
					profile={profile.current}
					current={setup}
					onconfirm={(next) => plain.setUp('graph', next as GraphSetup)}
					oncancel={setup ? () => plain.edit('graph', false) : undefined}
				/>
			{/if}
		</div>
	{:else if missing.length > 0}
		<div class="m-auto flex max-w-sm flex-col items-center gap-2 p-6 text-center" role="alert">
			<p class="text-sm font-medium">This graph cannot be drawn</p>
			<p class="text-muted-foreground text-xs">
				Its set-up names {missing.length === 1 ? 'a column' : 'columns'} the table no longer holds:
				<span class="text-foreground font-mono">{missing.join(', ')}</span>.
			</p>
			<Button size="sm" onclick={() => plain.edit('graph')}>Set up again</Button>
		</div>
	{:else if read?.error && !graph}
		<div class="grid h-full place-items-center">
			<LoadFailure
				what="The graph could not be drawn."
				detail={failureText(read.error)}
				onretry={() => void read.refresh()}
			/>
		</div>
	{:else if !graph || !laid}
		<p class="text-muted-foreground m-auto flex items-center gap-2 text-sm" role="status">
			<Loader2 class="size-4 animate-spin" /> Linking the rows…
		</p>
	{:else}
		{@const d = graph.diagnostics}
		<div
			class="border-border bg-card/40 flex flex-wrap items-center gap-2 border-b px-3 py-1.5 text-xs"
		>
			<span class="text-muted-foreground" role="status">
				{n(graph.nodes.length)} nodes and {n(graph.edges.length)} edges drawn{d.cut_edges > 0 || d.cut_nodes > 0
					? ` (the heaviest of ${n(d.nodes)} nodes and ${n(d.edges)} edges)`
					: ''}{extentNote(d.extent) ? ` · ${extentNote(d.extent)}` : ''}{d.unmatched_targets >
					0 &&
				setup?.shape ===
					'self'
					? ` · ${n(d.unmatched_targets)} references match no row`
					: ''}{d.dropped_weights > 0
					? ` · ${n(d.dropped_weights)} rows left out: their weight is no finite number`
					: ''}{d.clipped_keys > 0 ? ` · ${n(d.clipped_keys)} long ids cut` : ''}{d.mixed_types
					? ' · the from and to columns hold different kinds of value, so 1 and “1” are two nodes'
					: ''}
			</span>
			<Button class="ml-auto" variant="ghost" size="xs" onclick={() => plain.edit('graph')}>
				<Settings2 /> Change set-up
			</Button>
		</div>
		<div class="@container flex min-h-0 flex-1">
			<div class="flex min-h-0 min-w-0 flex-1 flex-col @2xl:flex-row">
				<div class="relative min-h-60 flex-1" bind:clientWidth={gw} bind:clientHeight={gh}>
					{#if gpuReason}
						<div
							class="flex h-full flex-col items-center justify-center gap-1 px-6 text-center text-sm"
						>
							<span class="text-destructive">{gpuReason}</span>
							<span class="text-muted-foreground"
								>The graph draws with WebGPU; the rows work without it.</span
							>
						</div>
					{:else if gpuReason === null && gw > 0 && gh > 0 && graph.nodes.length > 0}
						<GpuGraph
							nodesX={laid.x}
							nodesY={laid.y}
							{layoutVersion}
							{nodeRgb}
							nodeSize={laid.size}
							{nodeState}
							edges={laid.edges}
							fitBounds={fit}
							width={gw}
							height={gh}
							{background}
							{markerIndex}
							{onHover}
							{onPick}
						/>
					{:else if graph.nodes.length === 0}
						<p class="text-muted-foreground grid h-full place-items-center text-sm">
							These columns link no rows.
						</p>
					{/if}
					{#if hover}
						<div
							class="border-border bg-popover text-popover-foreground pointer-events-none fixed z-30 rounded-lg border px-2 py-1 text-xs shadow-md"
							style:left="{hover.x + 12}px"
							style:top="{hover.y + 12}px"
						>
							<span class="font-medium">{hover.name}</span>{#if hover.type}<span
									class="text-muted-foreground">{` · ${hover.type}`}</span
								>{/if}
						</div>
					{/if}
					{#if groups.length > 1 || (groups[0]?.name ?? '') !== ''}
						<div
							class="bg-card/85 border-border text-muted-foreground absolute bottom-2 left-2 flex max-w-[calc(100%-1rem)] flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border px-2 py-1 text-[0.7rem] backdrop-blur"
							aria-label="Node colours"
						>
							{#each groups.slice(0, 12) as g (g.name)}
								<span class="flex items-center gap-1">
									<span
										class="size-2 rounded-full"
										style:background="rgb({g.rgb.r},{g.rgb.g},{g.rgb.b})"
									></span>
									{g.name || '(none)'}
								</span>
							{/each}
						</div>
					{/if}
				</div>
				{#if detail}
					<aside
						aria-labelledby="plain-node-heading"
						class="border-border bg-card max-h-[45%] w-full shrink-0 overflow-auto border-t p-3 @2xl:max-h-none @2xl:w-72 @2xl:border-t-0 @2xl:border-l"
					>
						<div class="flex items-start justify-between gap-2">
							<h3 id="plain-node-heading" class="text-sm font-semibold break-words">
								{detail.node.name}
							</h3>
							<Button
								variant="ghost"
								size="icon-xs"
								aria-label="Close {detail.node.name}"
								onclick={() => plain.reset()}><X /></Button
							>
						</div>
						<div class="text-muted-foreground mt-1 flex flex-wrap gap-1 text-xs">
							{#if detail.node.name !== detail.node.id}
								<span class="bg-secondary rounded-full px-2 py-0.5 font-mono">{detail.node.id}</span
								>
							{/if}
							{#if detail.node.type}<span class="bg-secondary rounded-full px-2 py-0.5"
									>{detail.node.type}</span
								>{/if}
							<span class="bg-secondary rounded-full px-2 py-0.5"
								>{setup?.weight ? 'weight' : 'degree'} {n(detail.node.mentions)}</span
							>
						</div>
						<p class="text-muted-foreground mt-3 text-xs">
							{beside ? 'Beside the graph are' : 'The Rows panel lists'} the rows whose
							<span class="font-mono">{setup?.source}</span>
							or
							<span class="font-mono">{setup?.target}</span> is this node.
						</p>
						{#if detail.neighbours.length > 0}
							<h4
								class="text-muted-foreground mt-3 mb-1 text-xs font-medium tracking-wide uppercase"
							>
								Linked
							</h4>
							<div class="flex flex-wrap gap-1">
								{#each detail.neighbours as nb (`${nb.id}-${nb.out}`)}
									<button
										type="button"
										class="border-border bg-secondary hover:bg-muted/60 focus-visible:ring-ring/50 rounded-full border px-2 py-0.5 text-xs outline-none focus-visible:ring-3"
										title={nb.relations.join(', ') || nb.name}
										onclick={() => pick(nb.id, nb.name)}
									>
										<span aria-hidden="true">{nb.out ? '→ ' : '← '}</span><span class="sr-only"
											>{nb.out ? 'to ' : 'from '}</span
										>{nb.name}{#if nb.relations.length > 0}<span class="text-muted-foreground">{` · ${nb.relations.join(', ')}`}</span>{/if}
									</button>
								{/each}
							</div>
						{/if}
					</aside>
				{/if}
			</div>
			{#if beside}
				<!-- Maximized, the graph stands in for its page: the rows its node names, beside it. -->
				<div class="border-border w-[28rem] max-w-[45%] shrink-0 border-l">
					<RowsPanel />
				</div>
			{/if}
		</div>
	{/if}
</section>
