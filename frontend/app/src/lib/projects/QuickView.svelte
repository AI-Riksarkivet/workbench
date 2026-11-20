<script lang="ts">
	import MediaThumb from './MediaThumb.svelte';
	// QUICK VIEW — review one item beside the queue, without leaving it.
	//
	// A pre-labelled queue is REVIEWED, not drawn on: look at the image, check the suggested class is
	// right, accept it or send it back. Doing that today means clicking "Annotate →", landing on the
	// canvas route, deciding, and navigating back — losing the filter, the page and the selection
	// each time. Fifty-seven items is fifty-seven round trips through a page you did not want to
	// leave.
	//
	// Deliberately NOT the drawing canvas: it shows the item and its verdict actions, and an item that needs shapes drawn
	// belongs on the canvas.
	//
	// The pane is held to the window's height: its head (the item by name, its state), its actions and the walk stay
	// in view, and the picture, the suggestions and the notes scroll beneath them.
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import { ChevronLeft, ChevronRight, ExternalLink, X } from '@lucide/svelte';

	import { datasetVersionText, predictedLabels } from './item-columns.ts';
	import { EVENT_LABELS, taskStateVariant } from './presentation.ts';
	import { neighbours } from './quick-view.ts';
	import { taskImageUrl } from './task-media.ts';
	import PersonName from './PersonName.svelte';
	import type { People, TaskDetail } from './types.ts';

	let {
		open = $bindable(false),
		taskId = $bindable<string | null>(null),
		tasks,
		busy = false,
		onfire,
		canvasHref,
		canvasVerb,
		people = {},
		nameOf,
	}: {
		open?: boolean;
		/** Which task is open. Bindable so prev/next move it without the parent mediating. */
		taskId?: string | null;
		/** The rows currently in view — the TABLE's page, so prev/next walk exactly what the filter
		 *  and sort produced rather than whatever order the server returned. */
		tasks: TaskDetail[];
		busy?: boolean;
		/** Fire one of the task's own legal events. The parent owns the call so the drawer and the
		 *  row buttons share one code path and one refusal surface. */
		onfire?: (task: TaskDetail, event: string) => void;
		/** The item's labeling page, for one that needs shapes drawn. */
		canvasHref: (task: TaskDetail) => string;
		/** The names of the people the tasks name. */
		people?: People;
		/** What opening it does for this reader: annotate, or view read-only. */
		canvasVerb: (task: TaskDetail) => string;
		/** What the item is called, as the table calls it (its document's title and where in it). */
		nameOf: (task: TaskDetail) => string;
	} = $props();

	const nav = $derived(neighbours(tasks, taskId));
	const task = $derived(tasks.find((t) => t.task_id === taskId) ?? null);

	/** What the annotator says this reader may fire on the item (`legal_events`, the rule the table and the canvas read
	 *  too), minus the two that need typed input — a recipient, a note. Those stay on the row where their dialogs
	 *  live; a drawer that opened a second dialog on top of itself would be a worse answer than sending you back to
	 *  the row. */
	const events = $derived(
		(task?.legal_events ?? [])
			.map((e) => e.event)
			.filter((e) => e !== 'assign' && e !== 'request_changes' && e !== 'save_draft'),
	);
</script>

<!-- A PANE, not a dialog. `role="complementary"` (implicit on <aside>) rather than `dialog`: nothing
     here is modal any more — the queue stays interactive beside it, which is the entire point of the
     change — and announcing a non-modal region as a dialog tells a screen-reader user focus is
     trapped when it is not. -->
<aside
	class="flex max-h-[calc(100dvh-4rem)] min-h-0 flex-col gap-3 p-4"
	data-testid="quick-view"
	aria-label="Item review"
>
	{#if task}
		<div class="flex shrink-0 flex-col gap-1">
			<div class="flex items-center gap-2">
				<h2
					class="min-w-0 truncate text-sm font-medium"
					title={task.source.keys.join(', ')}
					data-testid="quick-name"
				>
					{nameOf(task)}
				</h2>
				<Badge variant={taskStateVariant(task.state)}>{task.state.replace('_', ' ')}</Badge>
				<!-- A pane has no backdrop to click and no Escape handler of its own, so it needs an
				     explicit way out — a Sheet got both for free. -->
				<Button
					variant="ghost"
					size="icon-sm"
					class="ml-auto shrink-0"
					data-testid="quick-close"
					title="Close the review pane"
					onclick={() => (open = false)}
				>
					<X class="size-4" />
				</Button>
			</div>
			<!-- WHERE THIS CAME FROM (#83): the dataset and its version at send, the reproducibility pin a publish
			     records. `@v` rather than the bare number so it cannot read as part of the dataset name. -->
			<p class="text-muted-foreground text-sm">
				{nav.position} of {nav.total} in view
				{#if task.source.where}
					· <span class="font-mono">{task.source.where}</span>{#if datasetVersionText(task)}<span
							class="font-mono"
							title="the source dataset's version at send time — the reproducibility pin a publish records"
							>@{datasetVersionText(task)}</span
						>{/if}
				{/if}
			</p>
		</div>

		<!-- The actions and the walk, above the item: always in view, however tall the picture. -->
		<div class="flex shrink-0 flex-wrap items-center gap-1" data-testid="quick-actions">
			{#each events as event (event)}
				<Button
					size="sm"
					variant={event === 'accept' ? 'default' : 'outline'}
					disabled={busy}
					data-testid="quick-{event}"
					onclick={() => onfire?.(task, event)}
				>
					{EVENT_LABELS[event] ?? event}
				</Button>
			{/each}
			<Button size="sm" variant="ghost" href={canvasHref(task)}>
				<ExternalLink class="size-3.5" />
				{canvasVerb(task)}
			</Button>
			<span class="ml-auto flex items-center gap-1">
				<Button
					size="sm"
					variant="outline"
					disabled={!nav.prev}
					data-testid="quick-prev"
					aria-label="Previous item"
					onclick={() => (taskId = nav.prev?.task_id ?? taskId)}
				>
					<ChevronLeft class="size-4" />
				</Button>
				<Button
					size="sm"
					variant="outline"
					disabled={!nav.next}
					data-testid="quick-next"
					aria-label="Next item"
					onclick={() => (taskId = nav.next?.task_id ?? taskId)}
				>
					<ChevronRight class="size-4" />
				</Button>
			</span>
		</div>

		<div class="flex min-h-0 flex-auto flex-col gap-3 overflow-y-auto">
			{#key task.task_id}
				{@const src = taskImageUrl(task)}
				<!-- Same component as the grid and the filmstrip (#69): a media-type glyph, never the
				     words "no preview". `object-contain` here — quick view is for READING the item, so it
				     must not crop. -->
				<MediaThumb
					{src}
					kind={task.media?.kind ?? 'image'}
					alt=""
					ratio="max-h-[45vh] rounded-md"
					fit="contain"
				/>
			{/key}

			<!-- The SUGGESTED label. `secondary`, never the default variant: the entire job of this
			     drawer is deciding whether the suggestion is right, so it must not already look accepted. -->
			{#if predictedLabels(task).length > 0}
				<div class="flex flex-wrap items-center gap-1 text-xs">
					<span class="text-muted-foreground">suggested:</span>
					{#each predictedLabels(task) as label (label)}
						<Badge variant="secondary">{label}</Badge>
					{/each}
				</div>
			{/if}

			{#if task.review_notes.length > 0}
				<ul class="text-muted-foreground flex flex-col gap-1 text-xs">
					{#each task.review_notes as note, i (i)}
						<li>
							<span class="font-medium"><PersonName principal={note.by} {people} />:</span>
							{note.message}
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	{:else}
		<!-- The lookup can legitimately miss, and the pane says so rather than rendering nothing: `tasks` is the
		     CURRENT PAGE's rows, and a filter, a sort or a page change between opening and rendering leaves the id
		     pointing at a row that is no longer in view. That is recoverable and must say so. -->
		<div class="flex flex-col gap-1">
			<div class="flex items-center gap-2">
				<h2 class="text-sm font-medium">Item not in view</h2>
				<Button
					variant="ghost"
					size="icon-sm"
					class="ml-auto shrink-0"
					data-testid="quick-close"
					title="Close the review pane"
					onclick={() => (open = false)}
				>
					<X class="size-4" />
				</Button>
			</div>
			<p class="text-muted-foreground text-sm">
				This item is no longer on the current page — a filter, sort or page change moved it. Clear
				the filter or turn back to its page to preview it.
			</p>
		</div>
	{/if}
</aside>
