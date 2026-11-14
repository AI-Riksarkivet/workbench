<script lang="ts">
	/**
	 * The dock's atlas: the dataset's embedding map (`#lib/atlas/AtlasMap.svelte`), linked both ways to
	 * the Results panel through `explore`. The search's hits light up on the map and the rest dims; a point
	 * clicked opens in the Player; a lasso or a legend pick can be shown as the results.
	 */
	import type { Hit } from '@workbench/explorer-api';
	import AtlasMap from '#lib/atlas/AtlasMap.svelte';
	import { crossFilter } from '#lib/atlas/cross-filter.svelte.ts';
	import { explore } from '#lib/explore/explore.svelte.ts';

	// The search's hits dim the map's other points: the map's filter follows the results, once the map
	// has indexed its points (a search run before the points arrived is applied when they do).
	$effect(() => {
		const hits = explore.browsing ? [] : explore.hits;
		const indexed = crossFilter.keyToIndex.size > 0;
		if (hits.length > 0 && indexed) crossFilter.setFilteredFromHits(hits);
		else crossFilter.clearFilter();
	});

	const active = (): Hit | null => explore.active;
	const setActive = (hit: Hit | null): void => {
		explore.active = hit;
	};
</script>

<section class="flex h-full min-h-0 flex-col" aria-labelledby="atlas-heading">
	<h2 id="atlas-heading" class="sr-only">Atlas</h2>
	<div class="min-h-0 flex-1">
		<AtlasMap
			bind:active={active, setActive}
			onSeedSearch={(rowids) => void explore.showMapSelection(rowids, rowids.length)}
		/>
	</div>
</section>
