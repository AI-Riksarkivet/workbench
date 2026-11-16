<script lang="ts">
	/**
	 * A panel open on a dataset that cannot feed it: a saved layout or a view that names it, or a link. It
	 * says why, in the words the "open a panel" menu shows beside it (`pane-capabilities`'s `panelGates`),
	 * and fetches nothing.
	 */
	let { title, reason }: { title: string; reason: string } = $props();
	const id = $props.id();
</script>

<section
	class="flex h-full flex-col items-center justify-center gap-1 p-6 text-center"
	aria-labelledby="unavailable-{id}"
>
	<h2 id="unavailable-{id}" class="text-sm font-medium">No {title.toLowerCase()} here</h2>
	<p class="text-muted-foreground max-w-sm text-xs">
		{title} is not available: {reason}. Close this panel, or open a dataset that declares it.
	</p>
</section>
