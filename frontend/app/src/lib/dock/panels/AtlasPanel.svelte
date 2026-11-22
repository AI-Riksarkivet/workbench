<script lang="ts">
	/**
	 * The dock's atlas: the dataset's embedding map (`#lib/atlas/AtlasMap.svelte`), linked both ways to the
	 * Results panel through `explore`. The search's hits light up on the map and the rest dims; a point
	 * clicked opens in the Player; a lasso or a legend pick can be shown as the results.
	 *
	 * Maximized, the panel stands in for the page it replaced: the map beside its own player, over a table
	 * of what the map has selected (a lasso, or a legend's cluster or category), which can be sent to a
	 * project whole. The selection and the colour choice are the map's store's
	 * (`#lib/atlas/cross-filter.svelte.ts`), so maximizing and restoring keeps them.
	 */
	import { page } from '$app/state';
	import { Inbox } from '@lucide/svelte';
	import type { Hit } from '@workbench/explorer-api';
	import { activeView } from '@workbench/explorer-api/descriptor';
	import { Button } from '@riksarkivet/ui/button';
	import { ResizableSplit } from '@riksarkivet/ui/resizable-split';
	import AtlasMap from '#lib/atlas/AtlasMap.svelte';
	import { crossFilter } from '#lib/atlas/cross-filter.svelte.ts';
	import { dockControl } from '#lib/dock/dock-control.svelte.ts';
	import ColumnsMenu from '#lib/explore/components/columns-menu.svelte';
	import HitTable, { TABLE_COLUMNS } from '#lib/explore/components/hit-table.svelte';
	import PlayerPane from '#lib/explore/components/player-pane.svelte';
	import { explore } from '#lib/explore/explore.svelte.ts';
	import { loadCols, persistCols } from '#lib/explore/table-columns.ts';
	import { projectOf } from '#lib/project.ts';
	import SendToProjectDialog from '#lib/projects/SendToProjectDialog.svelte';
	import { GatedAction } from '@riksarkivet/ui/gated-action';
	import { sendGate } from '#lib/explore/pane-capabilities.ts';
	import { itemName } from '#lib/projects/item-name.ts';
	import { namesByKey, selectionKeys } from '#lib/projects/send.ts';

	const view = activeView();
	const maximized = $derived(dockControl.maximized === 'atlas');

	// The search's hits dim the map's other points: the map's filter follows the results, once the map
	// has indexed its points (a search run before the points arrived is applied when they do).
	$effect(() => {
		const hits = explore.browsing ? [] : explore.hits;
		const indexed = crossFilter.keyToIndex.size > 0;
		if (hits.length > 0 && indexed) crossFilter.setFilteredFromHits(hits);
		else crossFilter.clearFilter();
	});

	const active = (): Hit | null => explore.active;
	/** A point opened from the map: docked, it plays in the Player panel, brought in front. */
	const setActive = (hit: Hit | null): void => {
		explore.active = hit;
		if (hit && !maximized) dockControl.reveal('player');
	};

	/** The selection's rows the map read (its first thousand), and how many points it holds. */
	let rows = $state.raw<Hit[]>([]);
	let selected = $state(0);

	const COLS_KEY = 'workbench-atlas-columns-v1';
	const offered = TABLE_COLUMNS();
	let shown = $state(
		loadCols(COLS_KEY, [
			'thumbnail',
			...view.metadataFields.map((m) => m.field),
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
	/** Why this dataset's rows cannot be sent to a project (`sendGate`), shown on the disabled control. */
	const sendBlocked = $derived(sendGate(page.data.facts ?? null));

	let width = $state(0);
	const narrow = $derived(width > 0 && width < 768);
	let narrowTab = $state<'selection' | 'player'>('selection');
	/** A row of the selection opened: narrow, the player is the tab beside the table. */
	function open(hit: Hit | null): void {
		explore.active = hit;
		if (hit && narrow) narrowTab = 'player';
	}
</script>

{#snippet map()}
	<AtlasMap
		bind:active={active, setActive}
		onSeedSearch={(rowids) => {
			void explore.showMapSelection(rowids, rowids.length);
			dockControl.reveal('results');
		}}
		onSelectionHits={(hits, total) => {
			rows = hits;
			selected = total;
		}}
	/>
{/snippet}

{#snippet player()}
	<section class="bg-muted/30 h-full min-h-0 overflow-auto" aria-label="Player">
		<PlayerPane
			hit={explore.active}
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
			<h3 id="atlas-selection-heading" class="text-foreground font-medium">Selection</h3>
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
					<GatedAction
						allowed={sendBlocked === null}
						action="Send to a project"
						reason={sendBlocked ?? ''}
					>
						<Button
							variant="outline"
							size="xs"
							data-testid="send-to-project-open"
							onclick={() => (sendOpen = true)}
						>
							<Inbox /> Send to a project
						</Button>
					</GatedAction>
				{/if}
				<ColumnsMenu columns={offered} {shown} ontoggle={toggleColumn} />
			</div>
		</div>
		<div class="min-h-0 flex-1 overflow-auto">
			{#if rows.length > 0}
				<HitTable hits={rows} active={explore.active} visible={shown} onselect={open} />
			{/if}
		</div>
	</section>
{/snippet}

<section class="flex h-full min-h-0 flex-col" aria-labelledby="atlas-heading">
	<h2 id="atlas-heading" class="sr-only">Atlas</h2>
	<div class="min-h-0 flex-1" bind:clientWidth={width}>
		{#if !maximized}
			{@render map()}
		{:else if width === 0}
			<!-- Measured on the first frame: the arrangement depends on the panel's own width. -->
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
</section>

<SendToProjectDialog
	bind:open={sendOpen}
	keys={selectionKeys(rows, (h) => view.keyPath(h))}
	names={namesByKey(
		rows,
		(h) => view.keyPath(h),
		(h) => itemName(view, h),
	)}
	total={selected}
	from={{ dataset: view.id, version: view.rowTableVersion, mediaKind: view.mediaKind }}
	origin="atlas"
	orgs={(page.data.me?.orgs ?? []).map((o: { org: string }) => o.org)}
	project={projectOf(page.url)}
/>
