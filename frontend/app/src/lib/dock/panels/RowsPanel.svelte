<script lang="ts">
	/**
	 * A plain table's rows: the dataset's tables to choose from, its rows a page at a time in the
	 * table's own order or sorted by a column Lance orders, a text search where a column carries a
	 * full-text index, and the rows ranked by similarity to one where a vector column carries a vector
	 * index (`#lib/plain/plain.svelte.ts`). Each cell is written by its column's kind; a row opens in
	 * the Row panel. What the table cannot do stays on screen, disabled, with the reason.
	 */
	import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Search, X } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';
	import { tableRows, type ColumnView } from '#lib/api/datasets.api.ts';
	import { plainPanes } from '#lib/explore/pane-capabilities.ts';
	import { cellText, numeric } from '#lib/plain/cells.ts';
	import { plain } from '#lib/plain/plain.svelte.ts';

	const facts = $derived(plain.facts);
	const table = $derived(plain.table);
	const read = $derived(
		facts && table
			? tableRows({ dataset: facts.id, table, rows: $state.snapshot(plain.rows) })
			: null,
	);
	const page = $derived(read?.current ?? null);
	const columns = $derived(page?.columns ?? []);
	const panes = $derived(plainPanes(columns, facts?.read_only ?? null));
	const tables = $derived(Object.keys(facts?.tables ?? {}).sort());

	let draft = $state('');

	const failure = (err: unknown): string =>
		typeof err === 'object' && err !== null && 'body' in err
			? String((err as { body: { message?: unknown } }).body.message)
			: String(err);

	const unsortable = (column: ColumnView): string =>
		`Lance does not order ${column.kind} columns, so ${column.name} cannot be sorted`;

	function submit(event: SubmitEvent): void {
		event.preventDefault();
		plain.search(draft);
	}

	function clearSearch(): void {
		draft = '';
		plain.reset();
	}

	function open(rowid: number): void {
		if (!page) return;
		plain.opened = { table: page.table, rowid, version: page.version, qualified: page.qualified };
	}

	const first = $derived(page && page.rows.length > 0 ? page.offset + 1 : 0);
	const last = $derived(page ? page.offset + page.rows.length : 0);
</script>

<section class="flex h-full min-h-0 flex-col" aria-labelledby="rows-heading">
	<h2 id="rows-heading" class="sr-only">Rows</h2>
	<div class="border-border flex flex-wrap items-center gap-2 border-b px-3 py-2">
		{#if tables.length > 1}
			<label class="flex items-center gap-1.5 text-xs">
				<span class="text-muted-foreground">Table</span>
				<select
					class="border-input bg-background focus-visible:ring-ring/50 h-7 rounded-md border px-1.5 text-xs outline-none focus-visible:ring-3"
					value={table ?? ''}
					onchange={(e) => plain.choose((e.currentTarget as HTMLSelectElement).value)}
				>
					{#each tables as name (name)}
						<option value={name}>{name}</option>
					{/each}
				</select>
			</label>
		{:else if table}
			<span class="font-mono text-xs font-medium">{table}</span>
		{/if}
		<form class="flex min-w-48 flex-1 items-center gap-1" role="search" onsubmit={submit}>
			<label class="relative flex flex-1 items-center">
				<span class="sr-only">Search the rows</span>
				<Search class="text-muted-foreground pointer-events-none absolute left-2 size-3.5" />
				<input
					type="search"
					data-dataset-search
					bind:value={draft}
					disabled={!('column' in panes.search)}
					aria-describedby={'why' in panes.search ? 'no-search' : undefined}
					placeholder={'column' in panes.search
						? `Search ${panes.search.column}…`
						: 'Text search needs a full-text index'}
					class="border-input bg-background focus-visible:ring-ring/50 h-7 w-full rounded-md border pr-2 pl-7 text-xs outline-none focus-visible:ring-3 disabled:cursor-not-allowed disabled:opacity-60"
				/>
			</label>
			{#if 'why' in panes.search}
				<span id="no-search" class="sr-only">Text search is not available: {panes.search.why}.</span
				>
			{/if}
		</form>
		{#if plain.rows.q || plain.rows.near !== null}
			<Button variant="outline" size="xs" onclick={clearSearch}>
				<X />
				{plain.rows.q ? `“${plain.rows.q}”` : `Like row ${plain.rows.near}`}
			</Button>
		{/if}
	</div>

	{#if facts && Object.keys(facts.unreadable).length > 0}
		<ul class="text-muted-foreground border-border border-b px-3 py-1.5 text-xs" role="status">
			{#each Object.entries(facts.unreadable) as [name, reason] (name)}
				<li><span class="font-mono">{name}</span> could not be opened: {reason}</li>
			{/each}
		</ul>
	{/if}

	<div class="min-h-0 flex-1 overflow-auto">
		{#if read?.error && !page}
			<p class="text-destructive p-3 text-sm" role="alert">
				The rows could not be read: {failure(read.error)}
			</p>
		{:else if !page}
			<p class="text-muted-foreground p-3 text-sm" role="status">Reading the rows…</p>
		{:else}
			{#if read?.error}
				<p class="text-destructive px-3 py-1.5 text-xs" role="alert">{failure(read.error)}</p>
			{/if}
			<table class="w-full border-collapse text-xs">
				<thead>
					<tr
						class="border-border bg-card text-muted-foreground sticky top-0 z-10 border-b text-left"
					>
						{#if plain.rows.q || plain.rows.near !== null}
							<th class="px-3 py-2 font-medium whitespace-nowrap"
								>{plain.rows.q ? 'Score' : 'Distance'}</th
							>
						{/if}
						{#each columns as column (column.name)}
							<th
								class={[
									'px-3 py-2 font-medium whitespace-nowrap',
									numeric(column.kind) && 'text-right',
								]}
								aria-sort={plain.rows.sort === column.name
									? plain.rows.order === 'asc'
										? 'ascending'
										: 'descending'
									: undefined}
							>
								{#if column.sortable && !plain.rows.q && plain.rows.near === null}
									<button
										type="button"
										class="hover:text-foreground focus-visible:ring-ring/50 inline-flex items-center gap-1 rounded outline-none focus-visible:ring-2"
										onclick={() => plain.sortBy(column.name)}
										title="Sort by {column.name}"
									>
										{column.name}
										{#if plain.rows.sort === column.name}
											{#if plain.rows.order === 'asc'}
												<ArrowUp class="text-primary size-3" />
											{:else}
												<ArrowDown class="text-primary size-3" />
											{/if}
										{/if}
									</button>
								{:else}
									<span
										title={column.sortable
											? 'A search or ranking keeps its own order'
											: unsortable(column)}>{column.name}</span
									>
								{/if}
								<span class="text-muted-foreground/70 block font-mono text-[10px] font-normal"
									>{column.kind}</span
								>
							</th>
						{/each}
					</tr>
				</thead>
				<tbody>
					{#each page.rows as row (row.rowid)}
						<tr
							class={[
								'border-border/60 hover:bg-muted/60 focus-visible:ring-ring/50 cursor-pointer border-b outline-none focus-visible:ring-2 focus-visible:ring-inset',
								plain.opened?.rowid === row.rowid && 'bg-muted',
							]}
							tabindex="0"
							aria-selected={plain.opened?.rowid === row.rowid}
							onclick={() => open(row.rowid)}
							onkeydown={(e) => {
								if (e.key === 'Enter' || e.key === ' ') {
									e.preventDefault();
									open(row.rowid);
								}
							}}
						>
							{#if plain.rows.q || plain.rows.near !== null}
								<td class="text-muted-foreground px-3 py-1.5 text-right align-top tabular-nums"
									>{row.score?.toFixed(3) ?? ''}</td
								>
							{/if}
							{#each columns as column (column.name)}
								<td
									class={[
										'px-3 py-1.5 align-top',
										column.kind === 'text' &&
											'max-w-[28rem] min-w-[12rem] [overflow-wrap:anywhere]',
										column.kind !== 'text' && 'whitespace-nowrap',
										numeric(column.kind) && 'text-right tabular-nums',
										(column.kind === 'vector' || column.kind === 'json') &&
											'max-w-[16rem] truncate font-mono text-[11px]',
										(column.kind === 'binary' || column.kind === 'blob') && 'text-muted-foreground',
									]}
									title={column.kind === 'vector' || column.kind === 'json'
										? cellText(column.kind, row.cells[column.name])
										: undefined}
								>
									<span class={[column.kind === 'text' && 'line-clamp-2']}
										>{cellText(column.kind, row.cells[column.name])}</span
									>
								</td>
							{/each}
						</tr>
					{:else}
						<tr>
							<td class="text-muted-foreground px-3 py-3" colspan={columns.length + 1}>
								{plain.rows.q ? 'No row matches this search.' : 'This table holds no rows.'}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>

	{#if page}
		<div
			class="border-border text-muted-foreground flex items-center justify-between gap-2 border-t px-3 py-1.5 text-xs"
		>
			<span role="status" class="tabular-nums">
				{first}–{last} of {page.total.toLocaleString()}
				{plain.rows.q ? 'matches' : plain.rows.near !== null ? 'nearest rows' : 'rows'}
			</span>
			<span class="flex items-center gap-1">
				<Button
					variant="ghost"
					size="icon-xs"
					aria-label="Previous page"
					disabled={page.offset === 0}
					onclick={() => plain.turn(page.offset - page.limit)}><ChevronLeft /></Button
				>
				<Button
					variant="ghost"
					size="icon-xs"
					aria-label="Next page"
					disabled={last >= page.total}
					onclick={() => plain.turn(page.offset + page.limit)}><ChevronRight /></Button
				>
			</span>
		</div>
	{/if}
</section>
