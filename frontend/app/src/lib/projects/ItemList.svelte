<script lang="ts">
	// A page of items as a list of links onto the canvas, each named by its document where its row is read, with its
	// state and who worked it: what the Browse and Review tabs list before a person walks them one at a time.
	import { Badge } from '@workbench/ui/badge';
	import { Button } from '@workbench/ui/button';
	import { ChevronLeft, ChevronRight } from '@lucide/svelte';

	import { corpusRows } from '#lib/api/projects.api.ts';
	import { datasetView } from '#lib/api/explore.api.ts';
	import { indexRows, rowFor, rowKeysFor } from './corpus-rows.ts';
	import { itemName } from './item-name.ts';
	import PersonName from './PersonName.svelte';
	import { taskStateVariant } from './presentation.ts';
	import type { People, TaskDetail } from './types.ts';

	let {
		tasks,
		people = {},
		href,
		page,
		pages,
		onpage,
		consensusN = 1,
		testid,
	}: {
		tasks: readonly TaskDetail[];
		people?: People;
		/** Where an item opens. */
		href: (task: TaskDetail) => string;
		/** 1-based, of `pages`. */
		page: number;
		pages: number;
		onpage: (page: number) => void;
		consensusN?: number;
		testid: string;
	} = $props();

	const rowReads = $derived(
		[...Map.groupBy(tasks, (t) => t.source.where ?? null)].map(([dataset, group]) =>
			corpusRows({ dataset, keys: rowKeysFor(group) }),
		),
	);
	const rowIndex = $derived(
		new Map(
			rowReads.flatMap((r) => [...indexRows(r.current?.rows ?? [], r.current?.keyFields ?? [])]),
		),
	);
	function nameOf(task: TaskDetail): string {
		const row = rowFor(task, rowIndex);
		const view = task.source.where ? datasetView(task.source.where).current : undefined;
		return (row && view ? itemName(view, row) : null) ?? task.source.keys[0] ?? task.task_id;
	}
	const replica = (task: TaskDetail): string | null =>
		task.replica_of ? `replica ${/-r(\d+)$/.exec(task.task_id)?.[1] ?? '?'}/${consensusN}` : null;
</script>

<ul class="flex flex-col divide-y rounded-md border" data-testid={testid}>
	{#each tasks as task (task.task_id)}
		<li class="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
			<!-- `href` is resolved by `linkWith`. -->
			<!-- eslint-disable svelte/no-navigation-without-resolve -->
			<a
				class="min-w-0 truncate font-medium underline-offset-2 hover:underline"
				href={href(task)}
				title={task.task_id}>{nameOf(task)}</a
			>
			<!-- eslint-enable svelte/no-navigation-without-resolve -->
			{#if replica(task)}
				<Badge variant="outline">{replica(task)}</Badge>
			{/if}
			<Badge variant={taskStateVariant(task.state)}>{task.state.replace('_', ' ')}</Badge>
			<span class="text-muted-foreground ml-auto text-xs">
				{#if task.submitted_by}
					by <PersonName principal={task.submitted_by} {people} />
				{:else if task.assignee}
					held by <PersonName principal={task.assignee} {people} />
				{/if}
				{#if task.reviewed_by}
					· reviewed by <PersonName principal={task.reviewed_by} {people} />
				{/if}
			</span>
		</li>
	{:else}
		<li class="text-muted-foreground px-3 py-6 text-center text-sm">No items match.</li>
	{/each}
</ul>
{#if pages > 1}
	<div class="flex items-center justify-end gap-2 text-xs">
		<span class="text-muted-foreground tabular-nums">Page {page} of {pages}</span>
		<Button
			variant="outline"
			size="icon-sm"
			aria-label="Previous page"
			disabled={page <= 1}
			onclick={() => onpage(page - 1)}
		>
			<ChevronLeft class="size-4" />
		</Button>
		<Button
			variant="outline"
			size="icon-sm"
			aria-label="Next page"
			disabled={page >= pages}
			onclick={() => onpage(page + 1)}
		>
			<ChevronRight class="size-4" />
		</Button>
	</div>
{/if}
