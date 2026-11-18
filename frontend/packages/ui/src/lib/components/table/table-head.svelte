<script lang="ts">
	import type { HTMLThAttributes } from 'svelte/elements';
	import { cn } from '../../utils/cn.js';
	import type { WithElementRef } from '../../utils/types.js';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...rest
	}: WithElementRef<HTMLThAttributes, HTMLTableCellElement> = $props();
</script>

<th
	bind:this={ref}
	data-slot="table-head"
	class={cn(
		'text-muted-foreground h-9 px-2.5 text-left align-middle text-[0.6875rem] font-normal tracking-[0.07em] whitespace-nowrap uppercase [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]',
		className,
	)}
	{...rest}
>
	{@render children?.()}
</th>
