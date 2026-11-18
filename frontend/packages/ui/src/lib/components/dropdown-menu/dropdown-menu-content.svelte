<script lang="ts">
	import { DropdownMenu as BitsDropdownMenu, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		sideOffset = 4,
		children,
		...rest
	}: WithoutChild<BitsDropdownMenu.ContentProps> = $props();
</script>

<BitsDropdownMenu.Portal>
	<BitsDropdownMenu.Content
		bind:ref
		{sideOffset}
		class={cn(
			'bg-popover text-popover-foreground data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 z-50 max-h-[var(--bits-dropdown-menu-content-available-height)] min-w-[8rem] origin-[var(--bits-dropdown-menu-content-transform-origin)] overflow-x-hidden overflow-y-auto rounded-none p-1 shadow-float',
			className,
		)}
		{...rest}
	>
		{@render children?.()}
	</BitsDropdownMenu.Content>
</BitsDropdownMenu.Portal>
