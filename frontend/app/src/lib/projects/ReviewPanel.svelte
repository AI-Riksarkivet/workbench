<script lang="ts">
	// REVIEW: a reviewer's fast pass over the work waiting for them, one item at a time on the canvas
	// (`?pass=review`): accept, send back with a note, or fix and accept on the canvas itself, by key, each verdict
	// moving on to the next item in review. Their own submissions are left out: the door refuses a self-review. Where
	// the project has replicas, the canvas sets the group's other replicas beside the item to compare.
	import { Button } from '@workbench/ui/button';
	import { Play } from '@lucide/svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';

	import { pageTasks } from '#lib/api/projects.api.ts';
	import { passHref } from '#lib/canvas/task-stream.ts';
	import { linkWith } from '#lib/project.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import ItemList from './ItemList.svelte';
	import {
		passParams,
		readItemQuery,
		withPassCount,
		writeItemQuery,
		type ItemQuery,
	} from './item-query.ts';
	import { ANNOTATOR_HINT } from './presentation.ts';
	import type { TaskDetail } from './types.ts';

	let {
		projectId,
		permissions,
		me,
		consensusN = 1,
	}: {
		projectId: string;
		permissions: readonly string[];
		me: string | null;
		consensusN?: number;
	} = $props();

	const mayReview = $derived(permissions.includes('can_review'));
	const query = $derived(readItemQuery(page.url.searchParams));
	const read = $derived(
		mayReview
			? pageTasks({
					projectId,
					filter: passParams('review', query, me).toString(),
					limit: query.size,
					offset: (query.page - 1) * query.size,
				})
			: null,
	);
	const listing = $derived(read?.current?.ok ? read.current.data : null);
	const tasks = $derived(listing?.details ?? []);
	const link = $derived(linkWith(projectId));
	// A pass started here counts what waits now (`of`, `item-query.readPassCount`), so the canvas reads "item 2 of 3"
	// as the reviewer moves through it, not the shrinking remainder.
	const href = (task: TaskDetail): string =>
		link(
			passHref(
				task,
				projectId,
				'review',
				withPassCount(writeItemQuery({ ...query, page: 1 }, new URLSearchParams()), {
					of: listing?.matched ?? null,
					done: 0,
				}),
			),
		);

	function show(next: ItemQuery): void {
		// oxlint-disable-next-line svelte/no-navigation-without-resolve -- the same path, its query changed
		void goto(`?${writeItemQuery(next, page.url.searchParams)}`, { reset: false });
	}

	/** Read the list again: the project page's Refresh, and the reader's return to the window. */
	export function reload(): void {
		void read?.refresh();
	}
</script>

<section class="flex flex-col gap-3" data-testid="review-panel">
	{#if !mayReview}
		<p class="text-muted-foreground text-sm" data-testid="review-not-yours">
			Reviewing is for this project's reviewers and managers.
		</p>
	{:else if read?.current && !read.current.ok}
		<LoadFailure
			what="The items in review could not be read."
			detail={read.current.detail}
			hint={ANNOTATOR_HINT}
			onretry={() => void read?.refresh()}
		/>
	{:else if !listing}
		<p class="text-muted-foreground text-sm" role="status">Reading the items in review…</p>
	{:else}
		<div class="flex flex-wrap items-center gap-3">
			<p class="text-sm" data-testid="review-waiting">
				{listing.matched === 0
					? 'Nothing is waiting for your review.'
					: `${listing.matched} item${listing.matched === 1 ? '' : 's'} wait${listing.matched === 1 ? 's' : ''} for your review.`}
			</p>
			{#if tasks[0]}
				<Button size="sm" href={href(tasks[0])} data-testid="review-start">
					<Play class="size-3.5" /> Start reviewing
				</Button>
			{/if}
		</div>
		<p class="text-muted-foreground max-w-3xl text-xs">
			On the canvas: <kbd>Shift+A</kbd> accepts, <kbd>Shift+F</kbd> accepts with your fixes,
			<kbd>Shift+R</kbd>
			sends the item back with a note, <kbd>Page Up</kbd> and <kbd>Page Down</kbd> step between items.
			Each verdict moves on to the next item.
		</p>
		{#if listing.matched > 0}
			<ItemList
				{tasks}
				people={listing.people}
				{href}
				{consensusN}
				page={query.page}
				pages={Math.max(1, Math.ceil(listing.matched / query.size))}
				onpage={(n) => show({ ...query, page: n })}
				testid="review-list"
			/>
		{/if}
	{/if}
</section>
