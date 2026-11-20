<script lang="ts">
	// The FILMSTRIP — your claimed items, down the left edge of the canvas.
	//
	// It is the VISUAL twin of the label stream (`TaskStreamNav`), not a second idea: same set, same
	// order, same `taskCanvasHref`, both computed by `task-stream.ts`. That sharing is the point — a
	// strip that built its own list would eventually disagree with the arrows about what "next" is,
	// and the person would have two answers on one screen with no way to tell which was right.
	//
	// What it adds over the arrows is SIGHT: the next item is visible rather than remembered, and you
	// can jump three ahead instead of pressing next three times. That is why both exist.
	//
	// Anchors, not buttons: a hop is a real URL, so it stays middle-clickable and reload-restorable,
	// exactly like the arrows.
	import { cn } from '@riksarkivet/ui/utils';
	import MediaThumb from '#lib/projects/MediaThumb.svelte';
	import { taskImageUrl } from '#lib/projects/task-media.ts';
	import type { TaskDetail } from '#lib/projects/types.ts';
	import { taskCanvasHref } from './task-stream.ts';

	let {
		items,
		activeTaskId,
		projectId,
		link,
		hrefOf = taskCanvasHref,
		offset = 0,
		total = items.length,
		label = 'Your claimed items',
	}: {
		/** The claimed stream, already filtered and ordered by `claimedStream`. */
		items: TaskDetail[];
		activeTaskId: string | null;
		projectId: string;
		/** Resolves an app path, carrying the reader's project (`linkWith`). */
		link: (path: string) => string;
		/** An item's app path on the walk the strip shows (a pass carries its filter). */
		hrefOf?: (task: TaskDetail, projectId: string) => string;
		/** Where the strip's first item sits in the whole walk, and how long the walk is. */
		offset?: number;
		total?: number;
		label?: string;
	} = $props();

	/** Keep the open item in view as you walk the queue with the arrows or the keyboard.
	 *
	 *  An attachment rather than an `$effect` over a captured node: the element is the thing the
	 *  behaviour belongs to, and it runs when THAT element mounts — which for a keyed list is exactly
	 *  when the active item changes. `block: 'nearest'` so an item already visible does not jolt the
	 *  strip. */
	function keepInView(node: HTMLElement) {
		node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
	}
</script>

{#if items.length > 0}
	<!-- Fixed width, its own scroll. `shrink-0` so the canvas gives up space rather than the strip:
	     a filmstrip that collapses to nothing under a narrow viewport is worse than one that scrolls,
	     because it silently removes navigation instead of making it smaller. -->
	<nav
		class="border-border bg-card/40 flex w-24 shrink-0 flex-col gap-1.5 overflow-y-auto border-r p-1.5"
		aria-label={label}
		data-testid="filmstrip"
	>
		{#each items as task, i (task.task_id)}
			{@const active = task.task_id === activeTaskId}
			<!-- `link` is `linkWith`, which resolves the path through `$app/paths`. -->
			<!-- eslint-disable svelte/no-navigation-without-resolve -->
			<a
				href={link(hrefOf(task, projectId))}
				class={cn(
					'group relative block overflow-hidden rounded-md border transition-colors',
					active
						? 'border-primary ring-primary/40 ring-2'
						: 'border-border hover:border-muted-foreground',
				)}
				aria-current={active ? 'true' : undefined}
				aria-label={`Item ${offset + i + 1} of ${total}`}
				data-testid="filmstrip-item"
				data-active={active ? 'true' : 'false'}
				{@attach active ? keepInView : () => {}}
			>
				<!-- eslint-enable svelte/no-navigation-without-resolve -->
				<MediaThumb src={taskImageUrl(task)} kind={task.media.kind} ratio="aspect-square" alt="" />
				<!-- The ORDINAL, not the task id. Walking a queue is positional — "item 3" is what the
				     arrows and the stream control both say, and a truncated uuid would agree with
				     neither. -->
				<span
					class="bg-background/80 text-muted-foreground absolute bottom-0.5 left-0.5 rounded px-1 text-[10px] tabular-nums backdrop-blur"
				>
					{offset + i + 1}
				</span>
			</a>
		{/each}
	</nav>
{/if}
