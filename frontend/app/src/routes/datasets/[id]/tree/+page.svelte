<script lang="ts">
	/**
	 * The dataset's topic hierarchy, as a zoomable treemap or a flow (Sankey), read from the viewer's
	 * topics door as the person (`#lib/api/topics.api.ts`). Choosing a topic lists its rows beside the map,
	 * playable in place; narrow (the page's own width), the rows cover the map until they are closed.
	 */
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import { ResizableSplit } from '@workbench/ui/resizable-split';
	import { topicTree } from '#lib/api/topics.api.ts';
	import { DATASET_PAGE_NAMES } from '#lib/dataset-nav.ts';
	import { failureText } from '#lib/failure.ts';
	import { titleOf } from '#lib/nav.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import TopicResultsPanel from '#lib/topics/topic-results-panel.svelte';
	import TopicTreemap from '#lib/topics/topic-treemap.svelte';

	let { data } = $props();
	const name = DATASET_PAGE_NAMES.tree;
	const dataset = $derived(data.id);
	const read = $derived(topicTree(dataset));

	/** The topic whose rows are shown; null shows the map alone. */
	let selectedTopic = $state<string | null>(null);

	let width = $state(0);
	const narrow = $derived(width > 0 && width < 768);

	/** The dataset's dock, filtered to `topic`, keeping the project and the table in view. */
	function searchHref(topic: string): string {
		const params = new URLSearchParams({ topic });
		for (const name of ['project', 'table']) {
			const value = page.url.searchParams.get(name);
			if (value) params.set(name, value);
		}
		return `${resolve('/datasets/[id]', { id: encodeURIComponent(dataset) })}?${params}`;
	}
</script>

<svelte:head><title>{titleOf(name, dataset, 'Datasets')}</title></svelte:head>

<h1 class="sr-only">{name}</h1>

<div class="relative min-h-0 flex-1" bind:clientWidth={width}>
	{#if read.error && !read.current}
		<div class="grid h-full place-items-center">
			<LoadFailure
				what="The topics could not be loaded."
				detail={failureText(read.error)}
				onretry={() => void read.refresh()}
			/>
		</div>
	{:else if !read.current}
		<div class="text-muted-foreground grid h-full place-items-center text-sm" role="status">
			Reading the topics…
		</div>
	{:else if !read.current.built || !read.current.hierarchy}
		<div class="grid h-full place-items-center p-6 text-center">
			<div class="max-w-sm">
				<p class="text-foreground text-sm font-medium">No topics yet</p>
				<p class="text-muted-foreground mt-1 text-xs">
					This dataset declares a topic hierarchy, but none has been written to its topics table
					yet.
				</p>
			</div>
		</div>
	{:else if width > 0}
		{@const tree = read.current.hierarchy}
		{@const noiseLabel = read.current.noise_label ?? ''}
		{#snippet map()}
			<TopicTreemap hierarchy={tree} {noiseLabel} onShowResults={(t) => (selectedTopic = t)} />
		{/snippet}
		{#snippet results(topic: string)}
			{#key topic}
				<TopicResultsPanel
					{dataset}
					{topic}
					searchHref={searchHref(topic)}
					onClose={() => (selectedTopic = null)}
				/>
			{/key}
		{/snippet}
		{#if narrow}
			<!-- The map stays mounted under the rows, so its drill and view survive a topic. -->
			<div class="h-full min-h-0">{@render map()}</div>
			{#if selectedTopic}
				<div class="absolute inset-0 z-10">{@render results(selectedTopic)}</div>
			{/if}
		{:else}
			<ResizableSplit storageKey="workbench-tree-split" initial={0.68} minLeft={420} minRight={300}>
				{#snippet left()}
					{@render map()}
				{/snippet}
				{#snippet right()}
					<div class="border-border h-full min-h-0 border-l">
						{#if selectedTopic}
							{@render results(selectedTopic)}
						{:else}
							<div
								class="bg-card text-muted-foreground grid h-full place-items-center p-6 text-center text-xs"
							>
								Choose a topic to list its rows here.
							</div>
						{/if}
					</div>
				{/snippet}
			</ResizableSplit>
		{/if}
	{/if}
</div>
