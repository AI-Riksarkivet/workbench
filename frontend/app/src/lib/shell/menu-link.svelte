<script lang="ts">
	import { goto } from '$app/navigation';
	import * as DropdownMenu from '@workbench/ui/dropdown-menu';
	import type { Snippet } from 'svelte';

	// A menu item that goes to an app page. Selecting it by pointer, Enter or Space navigates, as the
	// menu's own items do. It is drawn on `@workbench/ui`'s item, so it shares the menu's context: an item
	// from a second copy of bits-ui would find no menu to belong to.
	let {
		href,
		class: className = '',
		children,
	}: { href: string; class?: string; children: Snippet } = $props();
</script>

<DropdownMenu.Item class={className} onSelect={() => void goto(href)}>
	{@render children()}
</DropdownMenu.Item>
