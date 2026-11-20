<script lang="ts">
	// A2's honest lease chip. The wall clock is part of the truth here: a lease that lapsed between
	// server reminder ticks must read EXPIRED, never "held" — showing a corpse lease as held is how
	// an annotator loses work to a reclaim they were never warned about.
	import { Badge } from '@riksarkivet/ui/badge';

	import { wallClock } from './clock.svelte.ts';
	import { leaseView, formatLease } from './lease.ts';
	import { personName } from './people.ts';
	import type { People, TaskDetail } from './types.ts';

	let {
		task,
		me,
		people = {},
	}: {
		task: Pick<TaskDetail, 'state' | 'assignee' | 'lease_expires_at'>;
		me: string | null;
		people?: People;
	} = $props();

	// ONE CLOCK FOR THE WHOLE QUEUE, not one per row (`./clock.svelte.ts`): it holds the single interval and the POLL REASON (the wall clock is genuinely the only signal: no
	// event means "one more second passed").
	//
	// GUARDED OFF THE PROPS, deliberately not off `view`. `view` is derived FROM the clock, so
	// guarding on it would re-run this effect every tick and tear the subscription down and back up
	// once a second — the churn this change removes, reintroduced one layer up. A row that cannot
	// show a countdown subscribes to nothing, and a queue with no live leases runs no timer at all.
	$effect(() => {
		if (task.state !== 'claimed' || !task.lease_expires_at) return;
		return wallClock.subscribe();
	});

	const view = $derived(leaseView(task, me, wallClock.now));
</script>

{#if view.kind === 'held'}
	<Badge
		variant={view.mine ? 'default' : 'secondary'}
		title="{view.assignee}: lease expires in {formatLease(view.secondsLeft)}"
	>
		{view.mine ? 'yours' : personName(view.assignee, people)} · {formatLease(view.secondsLeft)}
	</Badge>
{:else if view.kind === 'pinned'}
	<Badge
		variant="secondary"
		title="{view.assignee}: assigned by a manager — the lease never expires"
	>
		{personName(view.assignee, people)} · pinned
	</Badge>
{:else if view.kind === 'expired'}
	<Badge
		variant="destructive"
		title="the lease lapsed — this claim is gone; the task returns to the queue"
	>
		{personName(view.assignee, people)} · expired
	</Badge>
{/if}
