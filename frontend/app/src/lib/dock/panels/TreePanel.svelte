<script module lang="ts">
	/** The treemap's layout, noise toggle and drill, kept across the dock remounting the panel (moved,
	 *  maximized, popped out) and started afresh for another dataset. */
	const kept = $state<{
		dataset: string | null;
		view: 'drill' | 'nested' | 'flow';
		showNoise: boolean;
		drill: string[];
	}>({ dataset: null, view: 'drill', showNoise: false, drill: [] });
</script>

<script lang="ts">
	/**
	 * The dataset's topic hierarchy as a dock panel: a zoomable treemap or a flow (Sankey), read from the
	 * viewer's topics door as the person (`#lib/api/topics.api.ts`). Choosing a topic is the dataset's
	 * search for it (`explore`, `topic=`): the Results panel lists its rows, the Atlas lights them up, and a
	 * row opens in the Player. Maximized, the panel stands in for the page it replaced and lists the topic's
	 * rows beside the map, playable in place.
	 */
	import { ResizableSplit } from '@riksarkivet/ui/resizable-split';
	import { Button } from '@riksarkivet/ui/button';
	import { X } from '@lucide/svelte';
	import { topicTree } from '#lib/api/topics.api.ts';
	import { dockControl } from '#lib/dock/dock-control.svelte.ts';
	import { explore } from '#lib/explore/explore.svelte.ts';
	import { failureText } from '#lib/failure.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import SearchRowsPanel from '#lib/explore/components/search-rows-panel.svelte';
	import TopicTreemap from '#lib/topics/topic-treemap.svelte';

	const dataset = $derived(explore.dataset ?? '');
	const read = $derived(topicTree(dataset));
	const chosen = $derived(explore.spec.topic ?? null);
	const maximized = $derived(dockControl.maximized === 'tree');

	// Another dataset's treemap starts at its root, in the default layout. The dataset page mounts its
	// panels afresh for each dataset, so this is read when the panel mounts.
	if (kept.dataset !== explore.dataset) {
		kept.dataset = explore.dataset;
		kept.view = 'drill';
		kept.showNoise = false;
		kept.drill = [];
	}

	let width = $state(0);
	const narrow = $derived(width > 0 && width < 768);

	/** Search the dataset for `topic`; docked, the Results panel comes in front to list it. */
	function choose(topic: string): void {
		explore.apply({ ...explore.spec, q: '', topic, image: undefined });
		if (!maximized) dockControl.reveal('results');
	}

	function clear(): void {
		explore.apply({ ...explore.spec, topic: undefined });
	}
</script>

<section class="flex h-full min-h-0 flex-col" aria-labelledby="tree-heading">
	<h2 id="tree-heading" class="sr-only">Tree</h2>
	{#if chosen && !maximized}
		<div
			class="border-border bg-card/40 flex shrink-0 items-center gap-2 border-b px-3 py-1.5 text-xs"
			role="status"
		>
			<span class="text-muted-foreground min-w-0 truncate"
				>The results list the topic <span class="text-foreground font-medium">{chosen}</span></span
			>
			<Button
				class="ml-auto"
				variant="ghost"
				size="xs"
				aria-label="Clear the topic {chosen}"
				onclick={clear}><X /> Clear</Button
			>
		</div>
	{/if}
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
				<TopicTreemap
					hierarchy={tree}
					{noiseLabel}
					onShowResults={choose}
					bind:view={kept.view}
					bind:showNoise={kept.showNoise}
					bind:drill={kept.drill}
				/>
			{/snippet}
			{#if !maximized}
				<div class="h-full min-h-0">{@render map()}</div>
			{:else if narrow}
				<!-- The map stays mounted under the rows, so its drill and view survive a topic. -->
				<div class="h-full min-h-0">{@render map()}</div>
				{#if chosen}
					<div class="absolute inset-0 z-10">
						<SearchRowsPanel title={chosen} empty="No rows carry this topic." onClose={clear} />
					</div>
				{/if}
			{:else}
				<ResizableSplit
					storageKey="workbench-tree-split"
					initial={0.68}
					minLeft={420}
					minRight={300}
				>
					{#snippet left()}
						{@render map()}
					{/snippet}
					{#snippet right()}
						<div class="border-border h-full min-h-0 border-l">
							{#if chosen}
								<SearchRowsPanel title={chosen} empty="No rows carry this topic." onClose={clear} />
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
</section>
