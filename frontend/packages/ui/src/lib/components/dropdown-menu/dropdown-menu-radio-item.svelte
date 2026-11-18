<script lang="ts">
	import { DropdownMenu as DropdownMenuPrimitive, type WithoutChild } from 'bits-ui';
	import type { Snippet } from 'svelte';
	import { cn } from '../../utils/cn.js';

	// A choice among several: the chosen one is marked by a square of the accent at the row's start (the block motif),
	// and Bits UI reports it as `menuitemradio` with `aria-checked`.
	let {
		ref = $bindable(null),
		class: className,
		children: childrenProp,
		...restProps
	}: WithoutChild<DropdownMenuPrimitive.RadioItemProps> & { children?: Snippet } = $props();
</script>

<DropdownMenuPrimitive.RadioItem
	bind:ref
	data-slot="dropdown-menu-radio-item"
	class={cn(
		'data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground relative flex cursor-default items-center gap-2 rounded-none py-1.5 pr-2 pl-7 text-sm outline-none select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0',
		className,
	)}
	{...restProps}
>
	{#snippet children({ checked })}
		<span
			aria-hidden="true"
			class={[
				'absolute left-2.5 size-2 transition-colors duration-(--duration-fast)',
				checked ? 'bg-primary' : 'bg-field-line/30',
			]}
		></span>
		{@render childrenProp?.()}
	{/snippet}
</DropdownMenuPrimitive.RadioItem>
