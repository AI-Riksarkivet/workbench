<script lang="ts">
	// BROWSE: a person looks through a project's labeled items (those whose work holds a shape or a link, the
	// annotator's `has_work`) without claiming any: filter by class and state (and,
	// for someone who may review, everyone's work rather than only their own), then walk the matches on the canvas,
	// read-only, with next and previous (`?pass=browse`, its filter in the address). The canvas reads each item's work
	// at the annotator's door, which decides on its own who may read it (a replica's work is its annotator's and its
	// reviewers').
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { Button } from '@workbench/ui/button';
	import { Select } from '@workbench/ui/select';
	import { Play } from '@lucide/svelte';

	import { pageTasks } from '#lib/api/projects.api.ts';
	import { passHref } from '#lib/canvas/task-stream.ts';
	import { linkWith } from '#lib/project.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import ItemList from './ItemList.svelte';
	import {
		LABELED_STATES,
		passParams,
		readItemQuery,
		writeItemQuery,
		type ItemQuery,
	} from './item-query.ts';
	import { ANNOTATOR_HINT } from './presentation.ts';
	import type { TaskDetail } from './types.ts';

	let {
		projectId,
		permissions,
		me,
		classes = [],
		consensusN = 1,
	}: {
		projectId: string;
		permissions: readonly string[];
		me: string | null;
		classes?: readonly string[];
		consensusN?: number;
	} = $props();

	const mayReview = $derived(permissions.includes('can_review'));
	const asked = $derived(readItemQuery(page.url.searchParams));
	// Someone who may not review another's work browses their own: the canvas's door would refuse the rest.
	const query = $derived<ItemQuery>(mayReview ? asked : { ...asked, whose: 'mine' });
	const read = $derived(
		pageTasks({
			projectId,
			filter: passParams('browse', query, me).toString(),
			limit: query.size,
			offset: (query.page - 1) * query.size,
		}),
	);
	const listing = $derived(read.current?.ok ? read.current.data : null);
	const tasks = $derived(listing?.details ?? []);
	const link = $derived(linkWith(projectId));
	const filterAddress = $derived(writeItemQuery({ ...query, page: 1 }, new URLSearchParams()));
	const href = (task: TaskDetail): string =>
		link(passHref(task, projectId, 'browse', filterAddress));

	function show(next: ItemQuery, keepPage = false): void {
		const address = writeItemQuery(keepPage ? next : { ...next, page: 1 }, page.url.searchParams);
		// oxlint-disable-next-line svelte/no-navigation-without-resolve -- the same path, its query changed
		void goto(`?${address}`, { reset: false });
	}

	/** Read the list again: the project page's Refresh, and the reader's return to the window. */
	export function reload(): void {
		void read.refresh();
	}
</script>

<section class="flex flex-col gap-3" data-testid="browse-panel">
	<p class="text-muted-foreground max-w-3xl text-sm">
		Look through the labeled items one at a time, without claiming them: the items whose work holds
		a shape or a link. The canvas shows each item's shapes and takes no edits.
	</p>
	<div class="flex flex-wrap items-center gap-2">
		{#if classes.length > 0}
			<Select
				value={query.label ?? ''}
				ariaLabel="Browse by class"
				onValueChange={(v) => show({ ...query, label: v || null })}
				options={[
					{ value: '', label: 'Any class' },
					...classes.map((c) => ({ value: c, label: c })),
				]}
			/>
		{/if}
		<Select
			value={query.states[0] ?? ''}
			ariaLabel="Browse by state"
			onValueChange={(v) => show({ ...query, states: v ? [v as ItemQuery['states'][number]] : [] })}
			options={[
				{ value: '', label: 'Any labeled state' },
				...LABELED_STATES.map((s) => ({ value: s, label: s.replace('_', ' ') })),
			]}
		/>
		{#if mayReview}
			<Select
				value={query.whose}
				ariaLabel="Whose work"
				onValueChange={(v) => show({ ...query, whose: v === 'mine' ? 'mine' : 'all' })}
				options={[
					{ value: 'all', label: "Everyone's work" },
					{ value: 'mine', label: 'My work' },
				]}
			/>
		{:else}
			<span class="text-muted-foreground text-xs"
				>Your own work: a reviewer browses everyone's.</span
			>
		{/if}
		{#if tasks[0]}
			<Button size="sm" href={href(tasks[0])} data-testid="browse-start">
				<Play class="size-3.5" /> Browse {listing?.matched ?? 0} item{listing?.matched === 1
					? ''
					: 's'}
			</Button>
		{/if}
	</div>
	{#if read.current && !read.current.ok}
		<LoadFailure
			what="The items could not be read."
			detail={read.current.detail}
			hint={ANNOTATOR_HINT}
			onretry={() => void read.refresh()}
		/>
	{:else if !listing}
		<p class="text-muted-foreground text-sm" role="status">Reading the items…</p>
	{:else}
		<ItemList
			{tasks}
			people={listing.people}
			{href}
			{consensusN}
			page={query.page}
			pages={Math.max(1, Math.ceil(listing.matched / query.size))}
			onpage={(n) => show({ ...query, page: n }, true)}
			testid="browse-list"
		/>
	{/if}
</section>
