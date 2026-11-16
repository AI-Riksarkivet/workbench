<script lang="ts">
	/**
	 * The atlas of a dataset with no workbench descriptor: there is no projection to draw, and one is not
	 * guessed from a vector column (which needs a reduction someone declares), so the panel says why.
	 */
	import { plainPanes } from '#lib/explore/pane-capabilities.ts';
	import { plain } from '#lib/plain/plain.svelte.ts';

	const why = $derived(plainPanes([], plain.facts?.read_only ?? null).atlas);
</script>

<section
	class="flex h-full flex-col items-center justify-center gap-1 p-6 text-center"
	aria-labelledby="plain-atlas-heading"
>
	<h2 id="plain-atlas-heading" class="text-sm font-medium">No atlas</h2>
	<p class="text-muted-foreground max-w-sm text-xs">
		The atlas is not available: {why}. A dataset whose writer stamps a workbench descriptor with an
		atlas space is drawn here.
	</p>
</section>
