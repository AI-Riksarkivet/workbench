<script lang="ts">
	/**
	 * Search in the dataset (the search service's Arrow rows) and the hits as cards, each with its
	 * thumbnail through a signed media link. Picking a card shows it in the Media panel.
	 *
	 * The submitted search and the pick live in `bench` (the dock's first invariant); the text being
	 * typed resets when the panel remounts, which is accepted.
	 */
	import { Search } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';
	import { Input } from '@workbench/ui/input';
	import { fetchDescriptor } from '#lib/api/datasets.api.ts';
	import { signedLinks, MAX_BATCH } from '#lib/api/media.api.ts';
	import { searchRows } from '#lib/api/search.api.ts';
	import type { Row } from '#lib/api/arrow.ts';
	import { bench } from '../bench.svelte.ts';

	// Seeded from the submitted search, so a remounted panel shows what it ran.
	let draft = $state(bench.query);

	const dataset = $derived(bench.dataset);
	const descriptor = $derived(dataset ? fetchDescriptor(dataset) : null);
	const docKey = $derived(descriptor?.current?.declared.identity.doc_key ?? null);
	const mimeColumn = $derived(descriptor?.current?.declared.document?.mime ?? null);
	const hasThumbnails = $derived(Boolean(descriptor?.current?.declared.document?.thumbnail));

	const hits = $derived(dataset && bench.query ? searchRows({ dataset, q: bench.query }) : null);

	/** A hit's doc id, as its row names it under the descriptor's doc key. */
	function docIdOf(row: Row): string | null {
		const id = docKey ? row[docKey] : null;
		return typeof id === 'string' || typeof id === 'number' ? String(id) : null;
	}

	const docIds = $derived(
		[...new Set((hits?.current ?? []).map(docIdOf).filter((id) => id !== null))].slice(
			0,
			MAX_BATCH,
		),
	);
	const thumbnails = $derived(
		dataset && hasThumbnails && docIds.length > 0
			? signedLinks({ dataset, role: 'thumbnail', docIds })
			: null,
	);

	/** The first short text a row carries besides its keys, as the card's caption. */
	function captionOf(row: Row): string | null {
		for (const [name, value] of Object.entries(row)) {
			if (name === docKey || name.startsWith('_')) continue;
			if (typeof value === 'string' && value.trim() !== '') return value;
		}
		return null;
	}

	function pick(row: Row): void {
		bench.selected = docIdOf(row);
		const mime = mimeColumn ? row[mimeColumn] : null;
		bench.selectedMime = typeof mime === 'string' ? mime : null;
	}

	function submit(event: SubmitEvent): void {
		event.preventDefault();
		bench.query = draft.trim();
	}

	const message = (err: unknown): string =>
		err instanceof Error
			? err.message
			: typeof err === 'object' && err !== null && 'body' in err
				? String((err as { body: { message?: unknown } }).body.message)
				: String(err);
</script>

<section class="flex h-full min-h-0 flex-col" aria-labelledby="results-heading">
	<h2 id="results-heading" class="sr-only">Results</h2>
	<form class="flex gap-2 border-b p-2" role="search" onsubmit={submit}>
		<label class="sr-only" for="results-query">Search this dataset</label>
		<Input
			id="results-query"
			bind:value={draft}
			placeholder="Search this dataset"
			autocomplete="off"
		/>
		<Button type="submit" size="sm" disabled={draft.trim() === ''}>
			<Search />
			Search
		</Button>
	</form>
	<div class="min-h-0 flex-1 overflow-auto p-2">
		{#if !bench.query}
			<p class="text-muted-foreground p-2 text-sm">Search the dataset to see its items here.</p>
		{:else if hits?.error && !hits.current}
			<div class="flex flex-col items-start gap-2 p-2">
				<p class="text-destructive text-sm" role="alert">Search failed: {message(hits.error)}</p>
				<Button variant="outline" size="sm" onclick={() => void hits?.refresh()}>Try again</Button>
			</div>
		{:else if !hits?.current}
			<p class="text-muted-foreground p-2 text-sm" role="status">Searching…</p>
		{:else if hits.current.length === 0}
			<p class="text-muted-foreground p-2 text-sm">No item matches “{bench.query}”.</p>
		{:else}
			<p class="text-muted-foreground px-1 pb-2 text-xs" role="status">
				{hits.current.length} items for “{bench.query}”
				{#if thumbnails?.error}
					· thumbnails unavailable: {message(thumbnails.error)}
				{/if}
			</p>
			<ul
				class="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-2"
				aria-label="Search results"
			>
				<!-- Keyed by position: one search answers one list, replaced whole by the next, and several rows
				     of one document (its chunks, its pages) share a doc id. -->
				{#each hits.current as row, index (index)}
					{@const id = docIdOf(row)}
					{@const thumb = id ? thumbnails?.current?.get(id) : undefined}
					<li>
						<button
							type="button"
							class="hover:bg-accent focus-visible:ring-ring/50 aria-pressed:border-primary flex w-full flex-col gap-1 rounded-lg border p-1.5 text-left outline-none focus-visible:ring-3"
							aria-pressed={id !== null && bench.selected === id}
							onclick={() => pick(row)}
						>
							<div
								class="bg-muted flex aspect-square w-full items-center justify-center overflow-hidden rounded-md"
							>
								{#if thumb && !('detail' in thumb)}
									<img
										src={thumb.url}
										alt={`Thumbnail of ${id}`}
										class="size-full object-cover"
										loading="lazy"
									/>
								{:else}
									<span class="text-muted-foreground px-2 text-center text-xs">
										{thumb ? thumb.detail : hasThumbnails ? '…' : 'No thumbnail'}
									</span>
								{/if}
							</div>
							<span class="truncate font-mono text-xs">{id ?? 'unnamed item'}</span>
							{#if captionOf(row)}
								<span class="text-muted-foreground line-clamp-2 text-xs">{captionOf(row)}</span>
							{/if}
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</section>
