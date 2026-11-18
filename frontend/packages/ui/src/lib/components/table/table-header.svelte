<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn } from '../../utils/cn.js';
	import type { WithElementRef } from '../../utils/types.js';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...rest
	}: WithElementRef<HTMLAttributes<HTMLTableSectionElement>, HTMLTableSectionElement> = $props();
</script>

<thead
	bind:this={ref}
	data-slot="table-header"
	class={cn('[&_tr]:border-border-strong [&_tr]:border-b [&_tr:hover]:bg-transparent', className)}
	{...rest}
>
	{@render children?.()}
</thead>
