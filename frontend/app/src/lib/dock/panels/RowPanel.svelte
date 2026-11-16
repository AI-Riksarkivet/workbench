<script lang="ts">
	/**
	 * One row of a plain table, in full, at the version its page was read at: every column by its kind,
	 * and an image or a recording previewed where a binary cell's bytes say they are one, through the
	 * viewer's signed media links (a signed URL for exactly that cell). Its actions: the rows most like
	 * it, where a vector column carries a vector index, and sending it to a project, which a plain table's
	 * row cannot be (it has no identity a task can name), each disabled with its reason when it cannot.
	 */
	import { Sparkles, Send } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';
	import { GatedAction } from '@workbench/ui/gated-action';
	import { mediaLinks } from '#lib/api/media.api.ts';
	import { tableRow, tableRows } from '#lib/api/datasets.api.ts';
	import { plainPanes } from '#lib/explore/pane-capabilities.ts';
	import { cellText } from '#lib/plain/cells.ts';
	import { plain } from '#lib/plain/plain.svelte.ts';

	const facts = $derived(plain.facts);
	const opened = $derived(plain.opened);
	const read = $derived(
		facts && opened
			? tableRow({
					dataset: facts.id,
					table: opened.table,
					rowid: opened.rowid,
					version: opened.version,
				})
			: null,
	);
	const row = $derived(read?.current ?? null);
	// The columns' kinds come from the page the row was opened from, which is held by the same read.
	const columns = $derived(
		facts && plain.table
			? (tableRows({ dataset: facts.id, table: plain.table, rows: $state.snapshot(plain.rows) })
					.current?.columns ?? [])
			: [],
	);
	const panes = $derived(plainPanes(columns, facts?.read_only ?? null));

	const failure = (err: unknown): string =>
		typeof err === 'object' && err !== null && 'body' in err
			? String((err as { body: { message?: unknown } }).body.message)
			: String(err);

	function preview(column: string): string | null {
		if (!facts || !opened) return null;
		const link = mediaLinks.link(facts.id, {
			role: 'cell',
			table: opened.qualified,
			column,
			row: opened.rowid,
			version: opened.version,
		});
		return link?.url ?? null;
	}
</script>

<section class="flex h-full min-h-0 flex-col overflow-auto" aria-labelledby="row-heading">
	{#if !opened}
		<h2 id="row-heading" class="sr-only">Row</h2>
		<p class="text-muted-foreground m-auto p-6 text-center text-sm">
			Choose a row to see it in full.
		</p>
	{:else}
		<header
			class="border-border flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2"
		>
			<h2 id="row-heading" class="text-sm font-medium">
				<span class="font-mono">{opened.table}</span>
				<span class="text-muted-foreground font-normal"
					>· row {opened.rowid} · v{opened.version}</span
				>
			</h2>
			<div class="flex items-center gap-1.5">
				<GatedAction
					allowed={!('why' in panes.similar)}
					action="Similar rows"
					reason={'why' in panes.similar ? panes.similar.why : ''}
				>
					<Button variant="outline" size="xs" onclick={() => plain.similarTo(opened.rowid)}
						><Sparkles /> Similar rows</Button
					>
				</GatedAction>
				<GatedAction
					allowed={panes.send === null}
					action="Send to a project"
					reason={panes.send ?? ''}
				>
					<Button variant="outline" size="xs"><Send /> Send to a project</Button>
				</GatedAction>
			</div>
		</header>
		{#if read?.error && !row}
			<p class="text-destructive p-3 text-sm" role="alert">
				The row could not be read: {failure(read.error)}
			</p>
		{:else if !row}
			<p class="text-muted-foreground p-3 text-sm" role="status">Reading the row…</p>
		{:else}
			<dl class="grid grid-cols-[minmax(6rem,max-content)_1fr] gap-x-4 gap-y-2 p-3 text-xs">
				{#each columns as column (column.name)}
					{@const media = row.media[column.name]}
					<dt class="text-muted-foreground pt-0.5">
						<span class="text-foreground font-medium">{column.name}</span>
						<span class="block font-mono text-[10px]">{column.arrow_type}</span>
					</dt>
					<dd class="min-w-0 [overflow-wrap:anywhere] whitespace-pre-wrap">
						{#if media?.startsWith('image/')}
							{@const url = preview(column.name)}
							{#if url}
								<img
									src={url}
									alt="{column.name} of row {opened.rowid}"
									class="bg-muted max-h-72 rounded-md border object-contain"
								/>
							{:else}
								<span class="text-muted-foreground">Loading the image…</span>
							{/if}
							<span class="text-muted-foreground mt-1 block"
								>{media} · {cellText(column.kind, row.cells[column.name])}</span
							>
						{:else if media?.startsWith('audio/')}
							{@const url = preview(column.name)}
							{#if url}
								<audio controls src={url} class="w-full max-w-md"></audio>
							{/if}
							<span class="text-muted-foreground mt-1 block"
								>{media} · {cellText(column.kind, row.cells[column.name])}</span
							>
						{:else if row.cells[column.name] === null || row.cells[column.name] === undefined}
							<span class="text-muted-foreground italic">null</span>
						{:else}
							<span class={[(column.kind === 'vector' || column.kind === 'json') && 'font-mono']}
								>{cellText(column.kind, row.cells[column.name])}</span
							>
						{/if}
					</dd>
				{/each}
			</dl>
		{/if}
	{/if}
</section>
