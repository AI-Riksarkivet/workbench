<script lang="ts">
	// The queue as a GRID of tiles — the mode you scan a corpus of page images in.
	//
	// A list is how you audit a queue; a grid is how you judge one. "Which of these forty are
	// letters" is one glance at thumbnails and forty rows of `fe00cd746463ad2c/1/12` in a table.
	// Label Studio's Data Manager opens image data in exactly this mode for exactly this reason.
	//
	// It shares ONE selection with the table (`rowSelection`, keyed by task id, bound from the
	// parent), so every bulk action works identically from either mode. That is what makes this a
	// second VIEW rather than a second, weaker table.
	import { Badge } from '@workbench/ui/badge';
	import { Checkbox } from '@workbench/ui/checkbox';
	import type { RowSelectionState } from '@workbench/ui/data-table';

	import { predictedLabels } from './item-columns.ts';
	import { taskStateVariant } from './presentation.ts';
	import { cardCaption } from './queue-view.ts';
	import { taskImageUrl } from './task-media.ts';
	import type { TaskDetail } from './types.ts';
	import MediaThumb from './MediaThumb.svelte';

	let {
		tasks,
		selection = $bindable({}),
		onopen,
	}: {
		/** The tasks to show — already filtered and sorted by the parent, so the grid and the table
		 *  can never disagree about WHICH items are in view. */
		tasks: TaskDetail[];
		/** The shared selection, keyed by task id. Bindable so ticking a tile is the same act as
		 *  ticking a row: the bulk bar reads one object either way. */
		selection?: RowSelectionState;
		/** Open one item for REVIEW beside the queue: a pane beside the table, not the drawing canvas. */
		onopen?: (task: TaskDetail) => void;
	} = $props();

	function toggle(taskId: string): void {
		// Rebuilt rather than mutated: `rowSelection` is TanStack's own state object, and replacing it
		// is what makes the table re-render when a tile is ticked.
		const next = { ...selection };
		if (next[taskId]) delete next[taskId];
		else next[taskId] = true;
		selection = next;
	}
</script>

<div
	class="grid grid-cols-[repeat(auto-fill,minmax(11rem,1fr))] gap-3"
	data-testid="task-grid"
	role="group"
	aria-label="Task grid"
>
	{#each tasks as task (task.task_id)}
		{@const src = taskImageUrl(task)}
		{@const picked = Boolean(selection[task.task_id])}
		<!-- The whole tile is the SELECT target, not just the checkbox: at this size the checkbox is a
		     16px hit area, and selecting is the action this mode exists for. Reviewing one item is the
		     secondary action and takes the explicit button below. -->
		<div
			class="border-border bg-card relative flex flex-col overflow-hidden rounded-lg border shadow-sm transition-colors {picked
				? 'border-primary ring-primary/40 ring-2'
				: 'hover:border-primary/60'}"
			data-testid="task-tile"
			data-selected={picked}
		>
			<button
				type="button"
				class="block w-full text-left"
				aria-label="Select {cardCaption(task)}"
				aria-pressed={picked}
				onclick={() => toggle(task.task_id)}
			>
				<!-- One component for every preview: `MediaThumb` answers a missing and a broken image alike
				     with the media type's own glyph. -->
				<MediaThumb {src} kind={task.media?.kind ?? 'image'} alt="" />
			</button>

			<div class="absolute top-1.5 left-1.5">
				<Checkbox
					checked={picked}
					onCheckedChange={() => toggle(task.task_id)}
					aria-label="Select"
				/>
			</div>

			<div class="flex flex-col gap-1 p-2">
				<div class="flex items-center justify-between gap-1">
					<span
						class="text-muted-foreground truncate font-mono text-xs"
						title={task.source.keys.join(', ')}
					>
						{cardCaption(task)}
					</span>
					<Badge variant={taskStateVariant(task.state)} class="shrink-0 text-[10px]">
						{task.state.replace('_', ' ')}
					</Badge>
				</div>

				<!-- The SUGGESTED label, `secondary` so it can never be mistaken for an accepted one. -->
				{#if predictedLabels(task).length > 0}
					<div class="flex flex-wrap gap-1">
						{#each predictedLabels(task) as label (label)}
							<Badge variant="secondary" class="text-[10px]" title="suggested — not reviewed">
								{label}
							</Badge>
						{/each}
					</div>
				{/if}

				{#if onopen}
					<button
						type="button"
						class="text-muted-foreground hover:text-foreground self-start text-xs underline underline-offset-2"
						onclick={() => onopen?.(task)}
					>
						Review →
					</button>
				{/if}
			</div>
		</div>
	{/each}
</div>
