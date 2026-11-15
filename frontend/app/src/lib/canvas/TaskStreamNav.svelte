<script lang="ts">
	// Between-ITEM navigation — the label stream, floating over the canvas with its other overlays.
	//
	// Anchors, not buttons-with-goto: a stream hop is a navigation to a real URL, so it must be
	// middle-clickable, copyable and restorable by reload — the canvas is URL-driven throughout.
	import { ChevronLeft, ChevronRight } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';

	import type { StreamPosition } from './task-stream.ts';

	let { stream }: { stream: StreamPosition } = $props();
</script>

{#if stream.active}
	<nav
		class="border-border bg-card/90 text-card-foreground pointer-events-auto absolute top-2 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1 rounded-lg border p-1 shadow-sm backdrop-blur"
		aria-label="Your labeling queue"
		data-testid="task-stream-nav"
	>
		<!-- A boundary is a DISABLED control, not a missing one: the arrow keeps its place so the
		     control does not resize as you walk the queue, and "you are at the end" is visible rather
		     than inferred from a gap. `Button href=` renders an anchor; with no href it renders a real
		     disabled <button>, which is why the boundary case is spelled as an absent href. -->
		{#if stream.prevHref}
			<Button
				variant="ghost"
				size="icon-xs"
				href={stream.prevHref}
				aria-label="Previous item in your queue"
				data-testid="task-stream-prev"
			>
				<ChevronLeft class="size-4" />
			</Button>
		{:else}
			<Button
				variant="ghost"
				size="icon-xs"
				disabled
				aria-label="This is the first item you claimed"
			>
				<ChevronLeft class="size-4" />
			</Button>
		{/if}

		<!-- Named "item", not a bare fraction: the page control below reads `3 / 12` too, and two
		     unlabelled fractions on one canvas is the ambiguity this control exists to remove. -->
		<span class="text-muted-foreground px-1 text-xs tabular-nums">
			item {stream.position} / {stream.total}
		</span>

		{#if stream.nextHref}
			<Button
				variant="ghost"
				size="icon-xs"
				href={stream.nextHref}
				aria-label="Next item in your queue"
				data-testid="task-stream-next"
			>
				<ChevronRight class="size-4" />
			</Button>
		{:else}
			<Button
				variant="ghost"
				size="icon-xs"
				disabled
				aria-label="This is the last item you claimed"
			>
				<ChevronRight class="size-4" />
			</Button>
		{/if}
	</nav>
{/if}
