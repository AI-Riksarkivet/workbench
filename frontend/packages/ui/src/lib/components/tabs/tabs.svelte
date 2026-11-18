<script lang="ts">
	// Adapted from Baby UI's `tabs` (https://github.com/kanakkholwal/baby-ui, Apache-2.0; see THIRD_PARTY.md in this
	// package).
	import { Tabs as TabsPrimitive } from 'bits-ui';
	import { cn } from '../../utils/cn.js';
	import { setTabs } from './context.js';
	import type { TabsOrientation, TabsSize, TabsVariant } from './variants.js';

	// The tab ROOT. `min-h-0` alongside `flex flex-col`: a tab panel is very often a scroll container (a queue, a
	// members list), and a flex child's automatic minimum size is its content's, so without it the panel grows past
	// the root instead of scrolling inside it.
	let {
		ref = $bindable(null),
		value = $bindable(''),
		variant = 'line',
		size = 'default',
		orientation = 'horizontal',
		class: className,
		...restProps
	}: Omit<TabsPrimitive.RootProps, 'value'> & {
		value?: string;
		variant?: TabsVariant;
		size?: TabsSize;
		orientation?: TabsOrientation;
	} = $props();

	setTabs({
		get value() {
			return value;
		},
		get variant() {
			return variant;
		},
		get size() {
			return size;
		},
		get orientation() {
			return orientation;
		},
	});
</script>

<TabsPrimitive.Root
	bind:ref
	bind:value
	{orientation}
	data-slot="tabs"
	class={cn(
		'flex min-h-0',
		orientation === 'vertical' ? 'flex-row items-start gap-4' : 'flex-col gap-3',
		className,
	)}
	{...restProps}
/>
