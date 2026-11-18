<script lang="ts">
	// Adapted from Baby UI's `tabs` (Apache-2.0; see THIRD_PARTY.md in this package). The selected state is Bits UI's
	// own (`aria-selected`), so it can never drift from the primitive's idea of which tab is open.
	import { Tabs as TabsPrimitive } from 'bits-ui';
	import { cn } from '../../utils/cn.js';
	import { getTabs } from './context.js';
	import { tabsFrame } from './variants.js';

	let {
		ref = $bindable(null),
		value,
		class: className,
		...restProps
	}: TabsPrimitive.TriggerProps = $props();

	const tabs = getTabs();
	const frame = $derived(
		tabsFrame({ variant: tabs.variant, size: tabs.size, orientation: tabs.orientation }),
	);
</script>

<TabsPrimitive.Trigger
	bind:ref
	{value}
	data-slot="tabs-trigger"
	data-tab={value}
	class={cn(frame.trigger(), className)}
	{...restProps}
/>
