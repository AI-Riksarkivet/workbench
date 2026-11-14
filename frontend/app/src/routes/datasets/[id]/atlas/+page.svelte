<script lang="ts">
	/**
	 * The dataset's atlas as a page of its own: the embedding map beside the player, over a table of
	 * what the map has selected (a lasso, or a legend's cluster or category). The map, its selection and
	 * its colour choice are the dock panel's too (`#lib/atlas/cross-filter.svelte.ts`), so either view
	 * picks up where the other left off.
	 *
	 * Wide, the map and the player sit side by side over the table. Below a tablet's width (sized by the
	 * page, not the window) the map takes the top and the table and the player share the rest as tabs.
	 */
	import { page } from '$app/state';
	import { Inbox } from '@lucide/svelte';
	import type { Hit } from '@workbench/explorer-api';
	import { Button } from '@workbench/ui/button';
	import SendToProjectDialog from '#lib/projects/SendToProjectDialog.svelte';
	import { selectionKeys } from '#lib/projects/send.ts';
	import { projectOf } from '#lib/project.ts';
	import { activeView } from '@workbench/explorer-api/descriptor';
	import { ResizableSplit } from '@workbench/ui/resizable-split';
	import AtlasMap from '#lib/atlas/AtlasMap.svelte';
	import ColumnsMenu from '#lib/explore/components/columns-menu.svelte';
	import HitTable, { TABLE_COLUMNS } from '#lib/explore/components/hit-table.svelte';
	import PlayerPane from '#lib/explore/components/player-pane.svelte';
	import { loadCols, persistCols } from '#lib/explore/table-columns.ts';
	import { DATASET_PAGE_NAMES } from '#lib/dataset-nav.ts';
	import { titleOf } from '#lib/nav.ts';

	let { data } = $props();
	const name = DATASET_PAGE_NAMES.atlas;

	let active = $state.raw<Hit | null>(null);
	let rows = $state.raw<Hit[]>([]);
	let selected = $state(0);

	const COLS_KEY = 'workbench-atlas-columns-v1';
	const offered = TABLE_COLUMNS();
	let shown = $state(
		loadCols(COLS_KEY, [
			'thumbnail',
			...activeView().metadataFields.map((m) => m.field),
			'start',
			'text',
			'caption',
		]).filter((key) => offered.some((c) => c.key === key)),
	);

	function toggleColumn(key: string): void {
		shown = shown.includes(key) ? shown.filter((k) => k !== key) : [...shown, key];
		persistCols(COLS_KEY, shown);
	}

	let sendOpen = $state(false);
	const view = activeView();

	function onSelectionHits(hits: Hit[], total: number): void {
		rows = hits;
		selected = total;
	}

	let width = $state(0);
	const narrow = $derived(width > 0 && width < 768);
	let narrowTab = $state<'selection' | 'player'>('selection');
	// Opening a point on a narrow page shows it: the player is the tab beside the table.
	const open = (hit: Hit | null): void => {
		active = hit;
		if (hit && narrow) narrowTab = 'player';
	};
</script>

<svelte:head><title>{titleOf(name, data.id, 'Datasets')}</title></svelte:head>

<h1 class="sr-only">{name}</h1>

<div class="min-h-0 flex-1" bind:clientWidth={width}>
	{#snippet map()}
		<AtlasMap bind:active={() => active, open} {onSelectionHits} />
	{/snippet}

	{#snippet player()}
		<section class="bg-muted/30 h-full min-h-0 overflow-auto" aria-label="Player">
			<PlayerPane
				hit={active}
				idle="Click a point on the map, or a row of the selection, to play it."
			/>
		</section>
	{/snippet}

	{#snippet selection()}
		<section
			class="bg-card/30 flex h-full min-h-0 flex-col"
			aria-labelledby="atlas-selection-heading"
		>
			<div class="border-border flex flex-wrap items-center gap-2 border-b px-3 py-1.5 text-xs">
				<h2 id="atlas-selection-heading" class="text-foreground font-medium">Selection</h2>
				<span class="text-muted-foreground min-w-0" role="status">
					{#if selected > 0}
						{selected.toLocaleString()}
						{selected === 1 ? 'row' : 'rows'}
						{#if selected > rows.length}· the first {rows.length.toLocaleString()} are listed{/if}
					{:else}
						Lasso a region, or pick a cluster in the legend, to list its rows.
					{/if}
				</span>
				<div class="ml-auto flex items-center gap-2">
					{#if selected > 0}
						<Button
							variant="outline"
							size="xs"
							data-testid="send-to-project-open"
							onclick={() => (sendOpen = true)}
						>
							<Inbox /> Send to a project
						</Button>
					{/if}
					<ColumnsMenu columns={offered} {shown} ontoggle={toggleColumn} />
				</div>
			</div>
			<div class="min-h-0 flex-1 overflow-auto">
				{#if rows.length > 0}
					<HitTable hits={rows} {active} visible={shown} onselect={open} />
				{/if}
			</div>
		</section>
	{/snippet}

	{#if width === 0}
		<!-- Measured on the first frame: the arrangement depends on the page's own width. -->
	{:else if narrow}
		<ResizableSplit
			orientation="vertical"
			storageKey="workbench-atlas-narrow-split"
			initial={0.55}
			minLeft={200}
			minRight={160}
		>
			{#snippet left()}
				{@render map()}
			{/snippet}
			{#snippet right()}
				<div class="border-border flex h-full min-h-0 flex-col border-t">
					<div
						role="tablist"
						aria-label="Below the map"
						class="border-border flex gap-1 border-b p-1"
					>
						{#each [['selection', 'Selection'], ['player', 'Player']] as const as [value, label] (value)}
							<button
								type="button"
								role="tab"
								id="atlas-tab-{value}"
								aria-selected={narrowTab === value}
								aria-controls="atlas-tabpanel"
								class={[
									'focus-visible:ring-ring/50 h-8 rounded-md px-3 text-sm outline-none focus-visible:ring-3',
									narrowTab === value
										? 'bg-secondary text-foreground'
										: 'text-muted-foreground hover:text-foreground',
								]}
								onclick={() => (narrowTab = value)}>{label}</button
							>
						{/each}
					</div>
					<div
						id="atlas-tabpanel"
						role="tabpanel"
						aria-labelledby="atlas-tab-{narrowTab}"
						class="min-h-0 flex-1"
					>
						{#if narrowTab === 'selection'}
							{@render selection()}
						{:else}
							{@render player()}
						{/if}
					</div>
				</div>
			{/snippet}
		</ResizableSplit>
	{:else}
		<ResizableSplit
			orientation="vertical"
			storageKey="workbench-atlas-vsplit"
			initial={0.66}
			minLeft={260}
			minRight={140}
		>
			{#snippet left()}
				<ResizableSplit
					storageKey="workbench-atlas-split"
					initial={0.66}
					minLeft={420}
					minRight={280}
				>
					{#snippet left()}
						{@render map()}
					{/snippet}
					{#snippet right()}
						{@render player()}
					{/snippet}
				</ResizableSplit>
			{/snippet}
			{#snippet right()}
				<div class="border-border h-full min-h-0 border-t">
					{@render selection()}
				</div>
			{/snippet}
		</ResizableSplit>
	{/if}
</div>

<SendToProjectDialog
	bind:open={sendOpen}
	keys={selectionKeys(rows, (h) => view.keyPath(h))}
	total={selected}
	from={{ dataset: view.id, version: view.rowTableVersion, mediaKind: view.mediaKind }}
	origin="atlas"
	orgs={(data.me?.orgs ?? []).map((o) => o.org)}
	project={projectOf(page.url)}
/>
