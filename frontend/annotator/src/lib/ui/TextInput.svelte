<script lang="ts">
	// The old annotator's text field: a mirror of the design system's input (same token classes, same h-8 /
	// rounded-lg / ring-3 geometry), kept in one file until this app is deleted (WB-109).
	import type { HTMLInputAttributes } from 'svelte/elements';
	import { cn } from '@riksarkivet/ui/utils';

	let {
		value = $bindable(''),
		class: className,
		...rest
	}: HTMLInputAttributes & { value?: string } = $props();
</script>

<input
	bind:value
	data-slot="input"
	class={cn(
		'dark:bg-input/30 border-input focus-visible:border-ring focus-visible:ring-ring/50 placeholder:text-muted-foreground h-8 w-full min-w-0 rounded-lg border bg-transparent px-2.5 py-1 text-sm transition-colors outline-none focus-visible:ring-3 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50',
		className,
	)}
	{...rest}
/>
