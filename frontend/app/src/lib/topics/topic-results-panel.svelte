<script lang="ts">
	/**
	 * The Tree page's results: the rows tagged with the chosen topic, listed and playable in place. "Open
	 * in search" continues to the dataset's dock with the same topic, to refine it there.
	 *
	 * A topic-only search (an empty query and `topic=`), read through `#lib/api/explore.api.ts` as the
	 * dock's `?topic=` link reads it.
	 */
	import { ArrowLeft, ArrowUpRight, X } from '@lucide/svelte';
	import type { Hit } from '@workbench/explorer-api';
	import { searchHits } from '#lib/api/explore.api.ts';
	import HitList from '#lib/explore/components/hit-list.svelte';
	import PlayerPane from '#lib/explore/components/player-pane.svelte';
	import { failureText } from '#lib/failure.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';

	let {
		dataset,
		topic,
		searchHref,
		onClose,
	}: {
		dataset: string;
		topic: string;
		/** The dataset's dock, filtered to the topic. */
		searchHref: string;
		onClose: () => void;
	} = $props();

	const BROWSE_N = 100;

	const read = $derived(
		searchHits({ dataset, spec: { q: '', topic, n: BROWSE_N, mode: 'fts', dataset } }),
	);
	const hits = $derived(read.current ?? []);
	let active = $state.raw<Hit | null>(null);
</script>

<section class="bg-card flex h-full min-h-0 flex-col" aria-labelledby="topic-results-heading">
	<header class="border-border flex h-11 shrink-0 items-center gap-2 border-b px-3">
		{#if active}
			<button
				type="button"
				onclick={() => (active = null)}
				aria-label="Back to the topic's rows"
				class="text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-ring/50 rounded-md p-1 transition-colors outline-none focus-visible:ring-3"
			>
				<ArrowLeft class="size-4" />
			</button>
		{/if}
		<h2
			id="topic-results-heading"
			class="text-foreground min-w-0 truncate text-sm font-medium"
			title={topic}
		>
			{topic}
		</h2>
		{#if read.current && !read.loading}
			<span class="text-muted-foreground shrink-0 text-xs">
				· {hits.length}{hits.length === BROWSE_N ? '+' : ''}
				{hits.length === 1 ? 'row' : 'rows'}
			</span>
		{/if}
		<!-- The parent resolves the route id (`/datasets/[id]`) and puts the topic query after it. -->
		<!-- eslint-disable svelte/no-navigation-without-resolve -->
		<a
			href={searchHref}
			class="border-border text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-ring/50 ml-auto inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-xs transition-colors outline-none focus-visible:ring-3"
			title="Refine this topic in the dataset's search"
		>
			Open in search <ArrowUpRight class="size-3" />
		</a>
		<button
			type="button"
			onclick={onClose}
			aria-label="Close the topic's rows"
			class="text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-ring/50 shrink-0 rounded-md p-1 transition-colors outline-none focus-visible:ring-3"
		>
			<X class="size-4" />
		</button>
	</header>

	<div class="min-h-0 flex-1 overflow-y-auto">
		{#if active}
			<PlayerPane hit={active} />
		{:else if read.error && !read.loading}
			<div class="flex justify-center">
				<LoadFailure
					what="The topic's rows could not be loaded."
					detail={failureText(read.error)}
					onretry={() => void read.refresh()}
				/>
			</div>
		{:else}
			<HitList
				{hits}
				{active}
				onselect={(h) => (active = h)}
				emptyMessage={read.current ? 'No rows carry this topic.' : 'Reading the topic…'}
			/>
		{/if}
	</div>
</section>
