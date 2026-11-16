<script lang="ts">
	/**
	 * A panel open on a dataset that cannot feed it: a saved layout or a view that names it, or a link. It
	 * says why, in the words the "open a panel" menu shows beside it (`pane-capabilities`'s `panelGates`),
	 * and fetches nothing. On a stamped dataset it also says that a set-up from picked columns is no way
	 * round it: set-ups apply to a table with no workbench descriptor.
	 */
	let {
		title,
		reason,
		plain = false,
	}: { title: string; reason: string; plain?: boolean } = $props();
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
	{#if !plain}
		<p class="text-muted-foreground max-w-sm text-xs">
			Setting it up from picked columns applies to a table with no workbench descriptor; this
			dataset has one, which declares what it draws.
		</p>
	{/if}
</section>
