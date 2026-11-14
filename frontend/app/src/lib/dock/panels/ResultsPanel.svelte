<script lang="ts">
	/**
	 * Search and browse one dataset: the search bar (text, semantic, image, voice), its filters and
	 * saved views, and the results as a list, a grid or a table; before any search, the dataset's
	 * documents, a page at a time. Picking a result opens it in the Player panel.
	 *
	 * What must survive a remount lives in `explore` (the dock's first invariant); the table's columns,
	 * widths and wrap and the grid's column count are the reader's preferences, kept in this browser.
	 */
	import { page } from '$app/state';
	import SignedImage from '#lib/explore/components/signed-image.svelte';
	import SendToProjectDialog from '#lib/projects/SendToProjectDialog.svelte';
	import { selectionKeys } from '#lib/projects/send.ts';
	import { projectOf } from '#lib/project.ts';
	import {
		AudioLines,
		Inbox,
		Lasso,
		ChevronLeft,
		ChevronRight,
		LayoutGrid,
		List as ListIcon,
		Loader2,
		Minus,
		Plus,
		RefreshCw,
		SearchX,
		Table as TableIcon,
		X,
	} from '@lucide/svelte';
	import { activeView, type Document } from '@workbench/explorer-api';
	import { Badge, Button } from '@workbench/ui';
	import { Switch } from '@workbench/ui/switch';
	import ActiveFilters from '#lib/explore/components/active-filters.svelte';
	import DocTile from '#lib/explore/components/doc-tile.svelte';
	import HitCard from '#lib/explore/components/hit-card.svelte';
	import HitList from '#lib/explore/components/hit-list.svelte';
	import HitTable, { TABLE_COLUMNS } from '#lib/explore/components/hit-table.svelte';
	import SavedViews from '#lib/explore/components/saved-views.svelte';
	import SearchBar from '#lib/explore/components/search-bar.svelte';
	import ColumnsMenu from '#lib/explore/components/columns-menu.svelte';
	import { explore, MAP_ROWS_MAX, PAGE_STEP, PER_PAGE } from '#lib/explore/explore.svelte.ts';
	import { thumbnailUrl } from '#lib/explore/media.ts';
	import {
		loadCols,
		loadTablePrefs,
		persistCols,
		persistTablePrefs,
	} from '#lib/explore/table-columns.ts';
	import { fmtTime, hitKey, makeHighlighter, queryTerms } from '#lib/explore/utils.ts';

	const ds = activeView();
	/** A dataset that declares no search is browsed only: no search bar is offered for it. */
	const searchable = ds.searchModes.length > 0;

	const hits = $derived(explore.hits);
	const active = $derived(explore.active);
	const activeKey = $derived(active ? hitKey(active) : null);
	const gridHighlight = $derived(makeHighlighter(queryTerms(explore.resultsQuery)));
	const docCount = $derived(new Set(hits.map((h) => ds.docId(h))).size);

	// Send to project: the rows the search answered, or the atlas selection shown as rows (all of it, listed or not).
	let sendOpen = $state(false);
	const sendKeys = $derived(selectionKeys(hits, (h) => ds.keyPath(h)));
	const sendTotal = $derived(explore.mapSelection?.points ?? hits.length);
	const orgs = $derived((page.data.me?.orgs ?? []).map((o: { org: string }) => o.org));

	const documents = $derived(explore.browsing ? explore.documents() : null);
	const docs = $derived(documents?.current?.docs ?? []);
	const docsTotal = $derived(documents?.current?.total ?? 0);
	const docsTotalPages = $derived(Math.max(1, Math.ceil(docsTotal / PER_PAGE)));
	const activeDocId = $derived(active ? ds.docId(active) : null);

	// ── Table preferences, kept in this browser ──
	const META_KEYS = ds.metadataFields.map((m) => m.field);
	const TABLE_COL_KEYS = TABLE_COLUMNS().map((c) => c.key);
	const DEFAULT_TABLE_COLS = [
		...(ds.isPlayable ? ['play'] : []),
		'thumbnail',
		'score',
		...META_KEYS,
		...(ds.hasCapability('diarization') ? ['speaker'] : []),
		...(ds.hasTime ? ['start', 'end', 'duration'] : []),
		...(ds.bodyField ? [ds.bodyField] : []),
		...(ds.captionField ? [ds.captionField] : []),
	];
	const TABLE_PREFS_KEY = 'workbench-results-table-v1';
	const initialTablePrefs = loadTablePrefs({
		storageKey: TABLE_PREFS_KEY,
		allKeys: TABLE_COL_KEYS,
		defaults: DEFAULT_TABLE_COLS,
	});
	let tableCols = $state<string[]>(initialTablePrefs.cols);
	let tableWidths = $state<Record<string, number>>(initialTablePrefs.widths);
	let tableWrap = $state<boolean>(initialTablePrefs.wrap);

	function persistTable(): void {
		persistTablePrefs(
			TABLE_PREFS_KEY,
			{ cols: tableCols, widths: tableWidths, wrap: tableWrap },
			TABLE_COL_KEYS,
		);
	}
	function setColWidth(key: string, width: number | null): void {
		tableWidths =
			width === null
				? Object.fromEntries(Object.entries(tableWidths).filter(([k]) => k !== key))
				: { ...tableWidths, [key]: width };
		persistTable();
	}
	function setTableWrap(wrap: boolean): void {
		tableWrap = wrap;
		persistTable();
	}
	/** The results table's columns a reader can choose; a dataset whose media does not play has no clip. */
	const hitColumns = TABLE_COLUMNS().filter((c) => c.key !== 'play' || ds.isPlayable);
	function toggleCol(key: string): void {
		tableCols = tableCols.includes(key) ? tableCols.filter((k) => k !== key) : [...tableCols, key];
		persistTable();
	}

	// The browse table's own columns: documents carry fewer fields than hits.
	const DOC_COLUMNS: { key: string; label: string; get: (d: Document) => string }[] = [
		{ key: 'thumbnail', label: 'Thumbnail', get: () => '' },
		...ds.metadataFields.map(({ field, label }) => ({
			key: field,
			label,
			get: (d: Document) => {
				const value = d[field];
				return value == null ? '' : String(value);
			},
		})),
		...(ds.hasTime
			? [
					{
						key: 'duration',
						label: 'Duration',
						get: (d: Document) => {
							const duration = ds.duration(d);
							return duration != null ? fmtTime(duration) : '';
						},
					},
				]
			: []),
	];
	const DOC_COLS_KEY = 'workbench-browse-table-v1';
	let docTableCols = $state<string[]>(
		loadCols(
			DOC_COLS_KEY,
			DOC_COLUMNS.map((c) => c.key),
		),
	);
	function toggleDocCol(key: string): void {
		docTableCols = docTableCols.includes(key)
			? docTableCols.filter((k) => k !== key)
			: [...docTableCols, key];
		persistCols(DOC_COLS_KEY, docTableCols);
	}

	const GRID_KEY = 'workbench-results-grid-cols';
	/** The reader's count, else two columns below a tablet's width and three above. */
	function loadGridCols(): number {
		const fallback = window.matchMedia('(width < 768px)').matches ? 2 : 3;
		try {
			const stored = localStorage.getItem(GRID_KEY);
			return stored ? Math.max(1, Math.min(6, Number(stored) || fallback)) : fallback;
		} catch {
			return fallback;
		}
	}
	let gridCols = $state<number>(loadGridCols());
	function setGridCols(n: number): void {
		gridCols = n;
		try {
			localStorage.setItem(GRID_KEY, String(n));
		} catch {
			// A browser that keeps no site storage keeps the count for this panel's life.
		}
	}

	const message = (err: unknown): string =>
		typeof err === 'object' && err !== null && 'detail' in err
			? String((err as { detail: unknown }).detail)
			: err instanceof Error
				? err.message
				: String(err);
</script>

{#snippet panelActions()}
	<Button
		variant="outline"
		size="sm"
		title="Read the results again: rows written since appear"
		disabled={explore.loadingHits}
		onclick={() => void explore.refresh()}
	>
		<RefreshCw />
		<span class="sr-only @2xl:not-sr-only">Refresh</span>
	</Button>
	{#if searchable}
		<SavedViews spec={explore.spec} onapply={(next) => explore.apply(next)} />
	{/if}
{/snippet}

<!-- A container: the controls fit the PANEL's width, which the dock changes independently of the
     window's (CSS container queries, `@lg` and up). -->
<section class="@container flex h-full min-h-0 flex-col" aria-labelledby="results-heading">
	<h2 id="results-heading" class="sr-only">Results</h2>
	<div class="border-border bg-card/40 border-b">
		{#if searchable}
			<SearchBar
				bind:spec={explore.spec}
				onsubmit={(next) => void explore.runSearch(next)}
				adoptSignal={explore.adopt}
				actions={panelActions}
			/>
		{:else}
			<div class="flex items-center gap-2 px-3 py-2">
				<p class="text-muted-foreground min-w-0 flex-1 text-sm">
					This dataset declares no search; its documents are listed below.
				</p>
				{@render panelActions()}
			</div>
		{/if}
		{#if searchable}
			<ActiveFilters bind:spec={explore.spec} onchange={(next) => void explore.runSearch(next)} />
		{/if}
		{#if explore.voiceActive}
			<div class="flex flex-wrap items-center gap-2 px-3 pb-3 text-xs">
				<Badge variant="secondary" class="gap-1.5 py-0.5 pr-0.5 pl-2">
					<AudioLines class="text-primary" />
					<span class="max-w-[24rem] truncate">
						Voice: {explore.voiceLabel ?? explore.voiceQuery?.doc_id ?? '…'}
						{#if explore.voiceQuery?.speaker_label}· {explore.voiceQuery.speaker_label}{/if}
					</span>
					<Button
						variant="ghost"
						size="icon-xs"
						class="size-4 rounded-full"
						title="Clear the voice search: back to the previous results"
						aria-label="Clear voice search"
						onclick={() => explore.dismissVoice()}
					>
						<X />
					</Button>
				</Badge>
				{#if explore.voiceQuery && explore.voiceQuery.turn_start != null && explore.voiceQuery.turn_end != null}
					<span class="text-muted-foreground">
						anchor turn {fmtTime(explore.voiceQuery.turn_start)}–{fmtTime(
							explore.voiceQuery.turn_end,
						)}
					</span>
				{/if}
				{#if explore.spec.q}
					<span class="text-muted-foreground w-full">
						Showing the voice's matches; the text search “{explore.spec.q}” returns when you clear
						it.
					</span>
				{/if}
			</div>
		{/if}
		{#if explore.mapSelection}
			<div class="flex flex-wrap items-center gap-2 px-3 pb-3 text-xs">
				<Badge variant="secondary" class="gap-1.5 py-0.5 pr-0.5 pl-2">
					<Lasso class="text-primary" />
					<span>
						Atlas selection: {explore.mapSelection.points.toLocaleString()}
						{explore.mapSelection.points === 1 ? 'point' : 'points'}
					</span>
					<Button
						variant="ghost"
						size="icon-xs"
						class="size-4 rounded-full"
						title="Clear the atlas selection: back to the search"
						aria-label="Clear atlas selection"
						onclick={() => explore.dismissMapSelection()}
					>
						<X />
					</Button>
				</Badge>
				{#if explore.mapSelection.points > MAP_ROWS_MAX}
					<span class="text-muted-foreground"
						>the first {MAP_ROWS_MAX.toLocaleString()} are listed</span
					>
				{/if}
			</div>
		{/if}
	</div>

	<div
		class="border-border bg-card/30 flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-3 py-2 text-xs"
	>
		<span class="text-muted-foreground min-w-0" role="status">
			{#if explore.browsing}
				{#if !(documents?.error && !documents.current)}
					Browsing {docsTotal} document{docsTotal === 1 ? '' : 's'}
				{/if}
			{:else if explore.loadingHits}
				Searching…
			{:else if explore.error && hits.length > 0}
				<span class="text-destructive">{explore.error}</span>
			{:else if explore.error}
				<!-- The failure is said once, below, beside the way to try again. -->
			{:else if explore.voiceActive}
				<strong class="text-foreground">{hits.length}</strong>
				{hits.length === 1 ? 'voice match' : 'voice matches'}
				{#if explore.allLoaded}<span class="text-muted-foreground">· all shown</span>{/if}
			{:else if hits.length === 0}
				No hits.
			{:else}
				<strong class="text-foreground">{hits.length}</strong>
				{hits.length === 1 ? 'chunk' : 'chunks'} across
				<strong class="text-foreground">{docCount}</strong>
				{docCount === 1 ? 'document' : 'documents'}
				{#if explore.allLoaded}<span class="text-muted-foreground">· all results shown</span>{/if}
			{/if}
		</span>

		<div class="ml-auto flex items-center gap-2">
			{#if !explore.browsing && hits.length > 0}
				<Button
					variant="outline"
					size="xs"
					data-testid="send-to-project-open"
					aria-label="Send to a project"
					onclick={() => (sendOpen = true)}
				>
					<Inbox /><span class="hidden @2xl:inline">Send to a project</span>
				</Button>
			{/if}
			{#if explore.layout === 'table' && explore.browsing}
				<ColumnsMenu columns={DOC_COLUMNS} shown={docTableCols} ontoggle={toggleDocCol} />
			{:else if explore.layout === 'table'}
				<ColumnsMenu columns={hitColumns} shown={tableCols} ontoggle={toggleCol}>
					<label class="text-muted-foreground flex cursor-pointer items-center gap-1.5 select-none">
						<Switch
							bind:checked={() => tableWrap, setTableWrap}
							aria-label="Wrap text in the text columns"
						/>
						<span>Wrap text</span>
					</label>
				</ColumnsMenu>
			{/if}
			{#if explore.layout === 'grid'}
				<div class="border-border flex items-center gap-1 border-r pr-2">
					<span class="text-muted-foreground mr-1 hidden @lg:inline">Per row</span>
					<Button
						variant="ghost"
						size="icon-xs"
						disabled={gridCols <= 1}
						title="Fewer columns"
						aria-label="Fewer grid columns"
						onclick={() => setGridCols(Math.max(1, gridCols - 1))}
					>
						<Minus />
					</Button>
					<span class="w-4 text-center font-mono text-xs">{gridCols}</span>
					<Button
						variant="ghost"
						size="icon-xs"
						disabled={gridCols >= 6}
						title="More columns"
						aria-label="More grid columns"
						onclick={() => setGridCols(Math.min(6, gridCols + 1))}
					>
						<Plus />
					</Button>
				</div>
			{/if}
			<div
				class="border-border bg-background flex items-center gap-0.5 rounded-lg border p-0.5"
				role="group"
				aria-label="Results layout"
			>
				<Button
					variant={explore.layout === 'list' ? 'secondary' : 'ghost'}
					size="icon-sm"
					title="List"
					aria-label="List"
					aria-pressed={explore.layout === 'list'}
					onclick={() => (explore.layout = 'list')}
				>
					<ListIcon />
				</Button>
				<Button
					variant={explore.layout === 'grid' ? 'secondary' : 'ghost'}
					size="icon-sm"
					title="Grid"
					aria-label="Grid"
					aria-pressed={explore.layout === 'grid'}
					onclick={() => (explore.layout = 'grid')}
				>
					<LayoutGrid />
				</Button>
				<Button
					variant={explore.layout === 'table' ? 'secondary' : 'ghost'}
					size="icon-sm"
					title="Table: each column's value per row"
					aria-label="Table"
					aria-pressed={explore.layout === 'table'}
					onclick={() => (explore.layout = 'table')}
				>
					<TableIcon />
				</Button>
			</div>
		</div>
	</div>

	<div class="relative min-h-0 flex-1 overflow-y-auto">
		{#if explore.browsing}
			{#if documents?.error && !documents.current}
				<div class="flex flex-col items-center gap-2 px-6 py-8 text-center text-sm">
					<p class="text-destructive" role="alert">
						The documents could not be read: {message(documents.error)}
					</p>
					<Button variant="outline" size="sm" onclick={() => void documents?.refresh()}>
						Try again
					</Button>
				</div>
			{:else if !documents?.current}
				<div class="text-muted-foreground flex items-center justify-center py-8 text-sm">
					<Loader2 class="mr-2 size-4 animate-spin" /> Reading the documents…
				</div>
			{:else if docs.length === 0}
				<p class="text-muted-foreground px-6 py-8 text-center text-sm">
					This dataset has no documents to browse.{searchable ? ' Search it instead.' : ''}
				</p>
			{:else if explore.layout === 'grid'}
				<ul
					class="grid gap-3 p-3"
					style:grid-template-columns="repeat({gridCols}, minmax(0, 1fr))"
					aria-label="Documents"
				>
					{#each docs as doc (ds.docId(doc))}
						<li>
							<DocTile
								{doc}
								active={activeDocId === ds.docId(doc)}
								onclick={() => explore.openDocument(doc)}
							/>
						</li>
					{/each}
				</ul>
			{:else if explore.layout === 'table'}
				{@const docCols = DOC_COLUMNS.filter((c) => docTableCols.includes(c.key))}
				<div class="overflow-x-auto">
					<table class="w-full border-collapse text-xs" aria-label="Documents">
						<thead>
							<tr
								class="border-border bg-card text-muted-foreground sticky top-0 z-10 border-b text-left"
							>
								{#each docCols as c (c.key)}
									<th class="px-3 py-2 font-medium whitespace-nowrap">{c.label}</th>
								{/each}
								<th class="px-3 py-2"><span class="sr-only">Open</span></th>
							</tr>
						</thead>
						<tbody>
							{#each docs as doc (ds.docId(doc))}
								{@const id = ds.docId(doc)}
								<tr
									class={'border-border/60 hover:bg-muted/60 border-b ' +
										(activeDocId === id
											? 'bg-primary/15 font-medium [box-shadow:inset_3px_0_0_0_var(--color-primary)]'
											: '')}
								>
									{#each docCols as c (c.key)}
										{#if c.key === 'thumbnail'}
											<td class="px-3 py-1.5 align-top">
												<SignedImage
													src={thumbnailUrl(doc)}
													class="bg-muted h-9 w-16 rounded-md object-cover"
												/>
											</td>
										{:else}
											<td
												class="text-muted-foreground max-w-[28rem] truncate px-3 py-1.5 align-top whitespace-nowrap"
												title={c.get(doc)}
											>
												{c.get(doc)}
											</td>
										{/if}
									{/each}
									<td class="px-3 py-1.5 text-right align-top">
										<Button
											variant="ghost"
											size="xs"
											aria-label={`Open ${ds.title(doc)}`}
											onclick={() => explore.openDocument(doc)}
										>
											Open
										</Button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{:else}
				<ul class="divide-border divide-y" aria-label="Documents">
					{#each docs as doc (ds.docId(doc))}
						<li>
							<button
								type="button"
								onclick={() => explore.openDocument(doc)}
								aria-pressed={activeDocId === ds.docId(doc)}
								class="hover:bg-muted/60 focus-visible:ring-ring aria-pressed:bg-primary/10 flex w-full items-center gap-3 px-4 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset"
							>
								<span class="min-w-0 flex-1 truncate text-sm">{ds.title(doc)}</span>
								<span class="text-muted-foreground shrink-0 truncate font-mono text-xs">
									{ds.metadata(doc).find((m) => m.value !== ds.title(doc))?.value ?? ''}
								</span>
							</button>
						</li>
					{/each}
				</ul>
			{/if}

			{#if docsTotalPages > 1}
				<nav
					class="border-border bg-card/80 sticky bottom-0 flex items-center justify-end gap-1 border-t px-4 py-2 text-xs backdrop-blur"
					aria-label="Document pages"
				>
					<span class="text-muted-foreground mr-2">Page {explore.docsPage} of {docsTotalPages}</span
					>
					<Button
						variant="outline"
						size="icon-sm"
						aria-label="Previous page"
						disabled={explore.docsPage <= 1}
						onclick={() => (explore.docsPage = Math.max(1, explore.docsPage - 1))}
					>
						<ChevronLeft />
					</Button>
					<Button
						variant="outline"
						size="icon-sm"
						aria-label="Next page"
						disabled={explore.docsPage >= docsTotalPages}
						onclick={() => (explore.docsPage = Math.min(docsTotalPages, explore.docsPage + 1))}
					>
						<ChevronRight />
					</Button>
				</nav>
			{/if}
		{:else if explore.loadingHits}
			<div class="text-muted-foreground flex items-center justify-center py-8 text-sm">
				<Loader2 class="mr-2 size-4 animate-spin" /> Searching…
			</div>
		{:else if explore.error && hits.length === 0}
			<div class="flex flex-col items-center gap-2 px-6 py-8 text-center text-sm">
				<p class="text-destructive" role="alert">Search failed: {explore.error}</p>
				<Button variant="outline" size="sm" onclick={() => void explore.refresh()}>Try again</Button
				>
			</div>
		{:else if hits.length === 0}
			<div
				class="text-muted-foreground flex flex-col items-center justify-center gap-2 px-6 py-8 text-center text-sm"
			>
				<SearchX class="text-muted-foreground size-6" />
				<p>No hits.</p>
				<p class="text-xs">
					{#if explore.voiceActive}
						No similar voices found. Try <strong>include the same document</strong> in Settings.
					{:else}
						Try <strong>Match by</strong> <em>Semantic</em>, or <strong>Style</strong>
						<em>Fuzzy</em>.
					{/if}
				</p>
			</div>
		{:else if explore.layout === 'grid'}
			<ul
				class="grid gap-3 p-3"
				style:grid-template-columns="repeat({gridCols}, minmax(0, 1fr))"
				aria-label="Search results"
			>
				{#each hits as hit (hitKey(hit))}
					<li>
						<HitCard
							{hit}
							query={explore.resultsQuery}
							highlight={gridHighlight}
							active={activeKey === hitKey(hit)}
							layout="tile"
							onclick={() => (explore.active = hit)}
						/>
					</li>
				{/each}
			</ul>
		{:else if explore.layout === 'table'}
			<HitTable
				{hits}
				{active}
				visible={tableCols}
				widths={tableWidths}
				wrap={tableWrap}
				onwidthchange={setColWidth}
				query={explore.resultsQuery}
				onselect={(hit) => (explore.active = hit)}
			/>
		{:else}
			<HitList
				{hits}
				query={explore.resultsQuery}
				{active}
				onselect={(hit) => (explore.active = hit)}
			/>
		{/if}

		{#if !explore.browsing && hits.length > 0 && !explore.allLoaded}
			<div class="border-border bg-card/40 flex justify-center border-t px-4 py-3">
				<Button
					variant="outline"
					size="sm"
					disabled={explore.loadingMore}
					onclick={() => void explore.loadMore()}
				>
					{explore.loadingMore
						? 'Loading…'
						: explore.voiceActive
							? 'Look for more voice matches'
							: `Show ${PAGE_STEP} more`}
				</Button>
			</div>
		{/if}
	</div>
</section>

<SendToProjectDialog
	bind:open={sendOpen}
	keys={sendKeys}
	total={sendTotal}
	from={{ dataset: ds.id, version: ds.rowTableVersion, mediaKind: ds.mediaKind }}
	origin={explore.mapSelection ? 'atlas' : 'search'}
	{orgs}
	project={projectOf(page.url)}
/>
