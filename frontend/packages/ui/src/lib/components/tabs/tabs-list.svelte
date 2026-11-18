<script lang="ts">
	// Adapted from Baby UI's `tabs` (https://github.com/kanakkholwal/baby-ui, Apache-2.0; see THIRD_PARTY.md in this
	// package): the sliding indicator measured from the selected trigger, and the scroll arrows a strip wider than its
	// column gets. Re-skinned to the tokens; motion is the tokens' durations, none under reduced motion.
	import { Tabs as TabsPrimitive } from 'bits-ui';
	import { on } from 'svelte/events';
	import { ChevronLeft, ChevronRight } from '@lucide/svelte';
	import { cn } from '../../utils/cn.js';
	import { getTabs } from './context.js';
	import { tabsFrame } from './variants.js';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: TabsPrimitive.ListProps = $props();

	const tabs = getTabs();
	const frame = $derived(
		tabsFrame({ variant: tabs.variant, size: tabs.size, orientation: tabs.orientation }),
	);

	let root = $state<HTMLDivElement>();
	let viewport = $state<HTMLDivElement>();
	type Rect = { left: number; top: number; width: number; height: number };
	let rects = $state<Record<string, Rect>>({});
	let edges = $state({ overflow: false, left: false, right: false });

	const indicator = $derived(rects[tabs.value] ?? { left: 0, top: 0, width: 0, height: 0 });
	const vertical = $derived(tabs.orientation === 'vertical');
	const scrollBehavior = (): ScrollBehavior =>
		matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';

	function measure(): void {
		if (!ref || !viewport || !root) return;
		const next: Record<string, Rect> = {};
		for (const el of ref.querySelectorAll<HTMLElement>('[data-tab]')) {
			const id = el.dataset['tab'];
			if (id)
				next[id] = {
					left: el.offsetLeft,
					top: el.offsetTop,
					width: el.offsetWidth,
					height: el.offsetHeight,
				};
		}
		rects = next;
		const max = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
		const from = Math.max(0, Math.min(max, Math.abs(viewport.scrollLeft)));
		edges = {
			overflow: viewport.scrollWidth > root.clientWidth + 1,
			left: from > 1,
			right: from < max - 1,
		};
	}

	$effect(() => {
		const list = ref;
		const port = viewport;
		if (!root || !port || !list) return;
		measure();
		const observer = new ResizeObserver(measure);
		observer.observe(root);
		observer.observe(list);
		// A trigger added or renamed later (a count in a label) moves the indicator too.
		const mutation = new MutationObserver(measure);
		mutation.observe(list, { childList: true, subtree: true, characterData: true });
		const off = on(port, 'scroll', measure, { passive: true });
		return () => {
			observer.disconnect();
			mutation.disconnect();
			off();
		};
	});

	// Keep the selected tab clear of the arrows over the faded edges.
	$effect(() => {
		const el = ref?.querySelector<HTMLElement>(`[data-tab="${CSS.escape(tabs.value)}"]`);
		if (!el || !viewport || !edges.overflow) return;
		const box = viewport.getBoundingClientRect();
		const item = el.getBoundingClientRect();
		const left = box.left + (edges.left ? 36 : 0);
		const right = box.right - (edges.right ? 36 : 0);
		const delta = item.left < left ? item.left - left : item.right > right ? item.right - right : 0;
		if (delta) viewport.scrollBy({ left: delta, behavior: scrollBehavior() });
	});

	const mask = $derived(
		edges.overflow
			? `linear-gradient(to right, ${edges.left ? 'transparent, black 40px' : 'black, black 0'}, ${
					edges.right ? 'black calc(100% - 40px), transparent' : 'black 100%'
				})`
			: undefined,
	);

	function scroll(direction: number): void {
		viewport?.scrollBy({
			left: direction * viewport.clientWidth * 0.8,
			behavior: scrollBehavior(),
		});
	}

	const ARROW =
		'text-foreground focus-visible:ring-ring absolute inset-y-0 z-20 inline-flex w-9 items-center justify-center transition-opacity hover:opacity-70 focus-visible:ring-2 focus-visible:outline-none disabled:pointer-events-none disabled:opacity-0';
</script>

<div
	bind:this={root}
	class={cn(
		'relative isolate flex w-full max-w-full min-w-0 items-center',
		vertical && 'w-auto shrink-0',
	)}
>
	{#if edges.overflow}
		<button
			type="button"
			aria-label="Scroll tabs left"
			disabled={!edges.left}
			onclick={() => scroll(-1)}
			class={cn(ARROW, 'left-0')}
		>
			<ChevronLeft class="size-4" aria-hidden="true" />
		</button>
	{/if}
	<div
		bind:this={viewport}
		style:mask-image={mask}
		style:-webkit-mask-image={mask}
		class="w-full min-w-0 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
	>
		<TabsPrimitive.List
			bind:ref
			data-slot="tabs-list"
			class={cn(frame.list(), className)}
			{...restProps}
		>
			<span
				aria-hidden="true"
				data-slot="tabs-indicator"
				style:transform={vertical
					? `translate(${indicator.left}px, ${indicator.top}px)`
					: `translateX(${indicator.left}px)`}
				style:width="{indicator.width}px"
				style:height={vertical ? `${indicator.height}px` : undefined}
				style:top={vertical ? '0' : undefined}
				style:bottom={vertical ? 'auto' : undefined}
				class={frame.indicator()}
			></span>
			{@render children?.()}
		</TabsPrimitive.List>
	</div>
	{#if edges.overflow}
		<button
			type="button"
			aria-label="Scroll tabs right"
			disabled={!edges.right}
			onclick={() => scroll(1)}
			class={cn(ARROW, 'right-0')}
		>
			<ChevronRight class="size-4" aria-hidden="true" />
		</button>
	{/if}
</div>
