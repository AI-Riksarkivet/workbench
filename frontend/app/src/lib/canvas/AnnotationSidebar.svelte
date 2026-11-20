<script lang="ts">
	// Right inspector pane: what the person is doing with the item (`head`, the task panel), then its annotations as a
	// list or the one selected, the layers and the history. Controlled by the controller.
	import type { Snippet } from 'svelte';
	import { ChevronLeft, List, Table } from '@lucide/svelte';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import { cn } from '@riksarkivet/ui/utils';
	import { STATUS_MEANING, statusDot, statusWord } from './statusStyle.ts';
	import AnnotationDetail from './AnnotationDetail.svelte';
	import AnnotationList from './AnnotationList.svelte';
	import AnnotationTable from './AnnotationTable.svelte';
	import BulkActions from './BulkActions.svelte';
	import LayerPanel from './LayerPanel.svelte';
	import VersionHistoryPanel from './VersionHistoryPanel.svelte';
	import type { AnnotatorController } from './controller.svelte.ts';

	let { controller, head }: { controller: AnnotatorController; head?: Snippet } = $props();

	// browse mode when nothing is selected: compact list vs full sortable table
	let view = $state<'list' | 'table'>('list');

	const summary = $derived.by(() => {
		const counts = new Map<string, number>();
		for (const r of controller.shapes) counts.set(r.status, (counts.get(r.status) ?? 0) + 1);
		return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
	});
</script>

<aside
	class="border-border bg-card flex h-full w-full min-w-0 flex-col border-l"
	data-testid="annotation-sidebar"
>
	{@render head?.()}
	<header class="border-border flex items-center justify-between gap-2 border-b px-3 py-2">
		<h2 class="text-sm font-medium">Annotations</h2>
		<div class="flex items-center gap-2">
			<!-- A segmented list/table switch: one bordered group, ghost buttons inside, so it reads
			     like the estate's other view toggles rather than two loose icon buttons. -->
			<div class="border-border flex overflow-hidden rounded-lg border p-0.5">
				<Button
					variant={view === 'list' ? 'secondary' : 'ghost'}
					size="icon-xs"
					title="List"
					aria-pressed={view === 'list'}
					onclick={() => (view = 'list')}
				>
					<List class="size-3.5" />
				</Button>
				<Button
					variant={view === 'table' ? 'secondary' : 'ghost'}
					size="icon-xs"
					title="Table"
					aria-pressed={view === 'table'}
					onclick={() => (view = 'table')}
				>
					<Table class="size-3.5" />
				</Button>
			</div>
			<Badge
				variant="outline"
				class="tabular-nums"
				title="Shapes on this item (a track counts once)"
				data-testid="shape-count"
			>
				{controller.shapes.length}
			</Badge>
		</div>
	</header>

	<!-- Each shape's own status, worded so it is not read as the item's review (the panel above says that). -->
	<div
		class="border-border flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b px-3 py-2"
		title={STATUS_MEANING}
		data-testid="shape-status"
	>
		{#if summary.length > 0}<span class="text-muted-foreground text-xs">Shapes</span>{/if}
		{#each summary as [status, n] (status)}
			<span class="text-muted-foreground flex items-center gap-1.5 text-xs">
				<span class={cn('size-2 rounded-full', statusDot(status))}></span>
				<span class="text-foreground tabular-nums">{n}</span>
				{statusWord(status)}
			</span>
		{/each}
	</div>

	<div class="flex min-h-0 flex-1 flex-col">
		{#if controller.multiSelect}
			{#if controller.selectedSet.size > 1 && controller.readOnly === null}
				<BulkActions {controller} />
			{/if}
			<div class="min-h-0 flex-1 overflow-y-auto">
				<AnnotationList {controller} />
			</div>
		{:else if controller.selected}
			<div class="border-border border-b px-2 py-1.5">
				<Button variant="ghost" size="sm" onclick={() => controller.select(null)}>
					<ChevronLeft class="size-3.5" /> Back to list
				</Button>
			</div>
			<div class="min-h-0 flex-1 overflow-y-auto">
				<AnnotationDetail {controller} />
			</div>
		{:else if view === 'table'}
			<AnnotationTable {controller} />
		{:else}
			<AnnotationList {controller} />
		{/if}
	</div>

	<LayerPanel {controller} />
	<VersionHistoryPanel {controller} />
</aside>
