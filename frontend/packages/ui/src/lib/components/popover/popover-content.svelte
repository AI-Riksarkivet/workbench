<script lang="ts">
	import { Popover as BitsPopover, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		sideOffset = 4,
		align = 'end',
		children,
		...rest
	}: WithoutChild<BitsPopover.ContentProps> = $props();
</script>

<BitsPopover.Portal>
	<BitsPopover.Content
		bind:ref
		{sideOffset}
		{align}
		class={cn(
			'bg-popover text-popover-foreground data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 z-50 w-72 origin-[var(--bits-popover-content-transform-origin)] rounded-none p-0 shadow-float outline-none',
			className,
		)}
		{...rest}
	>
		{@render children?.()}
	</BitsPopover.Content>
</BitsPopover.Portal>
