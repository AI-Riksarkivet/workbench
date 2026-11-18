<script lang="ts">
	import type { HTMLTextareaAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '../../utils/index.js';

	type Props = WithElementRef<HTMLTextareaAttributes>;

	let {
		ref = $bindable(null),
		value = $bindable(),
		class: className,
		'data-slot': dataSlot = 'textarea',
		...restProps
	}: Props = $props();
</script>

<textarea
	bind:this={ref}
	data-slot={dataSlot}
	class={cn(
		'bg-input border-field-line placeholder:text-muted-foreground hover:bg-accent/60 focus-visible:border-primary focus-visible:shadow-[inset_0_-1px_0_0_var(--primary)] aria-invalid:border-destructive aria-invalid:shadow-[inset_0_-1px_0_0_var(--destructive)] w-full min-w-0 rounded-none border-0 border-b px-2.5 text-base transition-[background-color,border-color,box-shadow] duration-(--duration-fast) outline-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm min-h-16 py-1.5',
		className,
	)}
	bind:value
	{...restProps}></textarea>
