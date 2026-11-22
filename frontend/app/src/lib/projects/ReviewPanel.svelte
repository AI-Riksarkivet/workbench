<script lang="ts">
	// REVIEW: a reviewer's fast pass over the work waiting for them, one item at a time on the canvas
	// (`?pass=review`): accept, send back with a note, or fix and accept on the canvas itself, by key, each verdict
	// moving on to the next item in review. Their own submissions are left out: the door refuses a self-review. Where
	// the project has replicas, the canvas sets the group's other replicas beside the item to compare.
	//
	// THE JUDGE (ADR 0026): where the project names one, the list is in its order (flagged first, then not judged yet,
	// then clear; `passParams`), each item marked with what it said, and the pass started here walks that order. The
	// items of the page no judge has read are asked about here, a few at once, and the list read again; a judge that
	// fails is said once and asked no more, and the list works as it does without one.
	import { Button } from '@riksarkivet/ui/button';
	import { Play } from '@lucide/svelte';
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';

	import { requestJudgement } from '#lib/api/judge.api.ts';
	import { pageTasks } from '#lib/api/projects.api.ts';
	import { passHref } from '#lib/canvas/task-stream.ts';
	import { linkWith } from '#lib/project.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import ItemList from './ItemList.svelte';
	import JudgeVerdict from './JudgeVerdict.svelte';
	import { judgeLine, TONE_CLASS } from './judge.ts';
	import { JudgeTopUp } from './judge-top-up.svelte.ts';
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
					judge: true,
				})
			: null,
	);
	const listing = $derived(read?.current?.ok ? read.current.data : null);
	const tasks = $derived(listing?.details ?? []);
	const judge = $derived(listing?.judge ?? null);
	const line = $derived(judge ? judgeLine(judge) : null);
	/** Whether the items carry the judge's marks: it can be asked, or some item already has its verdict. */
	const marked = $derived(
		judge?.available === true ||
			tasks.some((t) => t.judgement !== null && t.judgement !== undefined),
	);

	// THE TOP-UP (`JudgeTopUp`): the page's items no judge has read are asked about while the list is open, and it stops
	// when the panel goes.
	const topUp = new JudgeTopUp(requestJudgement, async () => read?.refresh());
	$effect(() => () => topUp.stop());
	$effect(() => {
		if (!judge?.available) return;
		const listed = tasks;
		untrack(() => void topUp.offer(listed));
	});
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

{#snippet verdict(task: TaskDetail)}
	{#if task.judgement}
		<JudgeVerdict judgement={task.judgement} />
	{:else if judge?.available}
		<span class="text-muted-foreground text-xs" data-testid="judge-unread">Not judged yet</span>
	{/if}
{/snippet}

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
		{#if topUp.failure !== null}
			<p class={TONE_CLASS.warn} role="alert" data-testid="review-judge">
				The judge is unavailable: {topUp.failure}
			</p>
		{:else if line && listing.matched > 0}
			<p class={TONE_CLASS[line.tone]} data-testid="review-judge">
				{line.text}{topUp.judging ? ' Asking the judge…' : ''}
			</p>
		{/if}
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
				aside={marked ? verdict : undefined}
			/>
		{/if}
	{/if}
</section>
