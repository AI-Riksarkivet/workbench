<script lang="ts" module>
	import type { ActivityEntryLike } from './nav-config.ts';

	/** One day's entries, newest first, under the day's heading. */
	type DayGroup = { key: string; heading: string; entries: ActivityEntryLike[] };

	const dayKey = (at: number): string => new Date(at).toDateString();

	/** Entries (already newest first) folded into days, each headed "Today", "Yesterday" or its date. */
	function groupByDay(entries: readonly ActivityEntryLike[], now: number): DayGroup[] {
		const today = dayKey(now);
		const yesterday = dayKey(now - 24 * 60 * 60 * 1000);
		const groups: DayGroup[] = [];
		for (const entry of entries) {
			const key = dayKey(entry.at);
			let group = groups.at(-1);
			if (group?.key !== key) {
				const heading =
					key === today
						? 'Today'
						: key === yesterday
							? 'Yesterday'
							: new Date(entry.at).toLocaleDateString(undefined, {
									weekday: 'long',
									month: 'short',
									day: 'numeric',
								});
				group = { key, heading, entries: [] };
				groups.push(group);
			}
			group.entries.push(entry);
		}
		return groups;
	}

	/** "Failed (HTTP 409)", "Failed (unreachable)" or "Failed". */
	function failureLabel(status: number | null): string {
		if (status === null) return 'Failed';
		if (status === 0) return 'Failed (unreachable)';
		return `Failed (HTTP ${status})`;
	}
</script>

<script lang="ts">
	import { Bell, CircleCheck, CircleX } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';
	import { HoldToConfirm } from '@workbench/ui/hold-to-confirm';
	import { RollingDigits } from '@workbench/ui/rolling-digits';
	import * as Popover from '@workbench/ui/popover';
	import { cn } from '@workbench/ui/utils';
	import { formatZoned } from './timestamp.ts';

	// The bell: the reader's OWN activity, the outcome of every write the app sent for them, kept
	// in their browser by the app. It does not fetch and does not store: the app hands the
	// entries in and hears back when the panel opens (every entry is then seen) and when the reader
	// clears the log.
	let {
		entries,
		unread,
		onopen,
		onclear,
		clock = Date.now,
		class: className,
	}: {
		/** Newest first. */
		entries: readonly ActivityEntryLike[];
		/** Entries recorded since the panel was last opened: the badge. */
		unread: number;
		onopen?: () => void;
		onclear?: () => void;
		/** Injectable clock for the day headings, read each time the panel opens. */
		clock?: () => number;
		class?: string;
	} = $props();

	// "Today" is the day the panel was opened on, read at the opening, so a tab left open past
	// midnight files yesterday's writes under Yesterday.
	let openedAt = $state(0);
	const groups = $derived(groupByDay(entries, openedAt));
	// A count, not an inventory: past two digits the exact number stops being information.
	const badge = (n: number): string => (n > 99 ? '99+' : String(n));
	// Clearing the log asks for a held press or a second one: a stray click would lose the reader's record. Reset on each
	// opening.
	let cleared = $state(false);
</script>

<Popover.Root
	onOpenChange={(open) => {
		if (!open) return;
		cleared = false;
		openedAt = clock();
		onopen?.();
	}}
>
	<Popover.Trigger>
		{#snippet child({ props })}
			<Button
				{...props}
				variant="ghost"
				size="icon"
				class={cn('relative', className)}
				aria-label={unread > 0 ? `Activity, ${unread} new` : 'Activity'}
			>
				<Bell class="size-4" aria-hidden="true" />
				{#if unread > 0}
					<!-- The button's name already says the number, so the count does not announce itself again. -->
					<span
						data-slot="activity-count"
						class="bg-primary text-primary-foreground absolute top-0 right-0 flex h-4 min-w-4 items-center justify-center px-1 text-[0.625rem] leading-none tracking-normal normal-case"
					>
						<RollingDigits value={unread} format={badge} live={false} />
					</span>
				{/if}
			</Button>
		{/snippet}
	</Popover.Trigger>
	<!-- bits-ui renders the content as a bare div with no role and no name, while the trigger
	     advertises `aria-haspopup="dialog"`; it traps focus and closes on Escape, so `dialog` is the
	     honest role for it. -->
	<Popover.Content
		class="w-96 max-w-[calc(100vw-2rem)] p-0"
		align="end"
		role="dialog"
		aria-label="Activity"
	>
		<div class="border-border flex items-center gap-2 border-b px-4 py-3">
			<div class="grid gap-0.5">
				<p class="text-lg leading-none">Activity</p>
				<span class="text-muted-foreground text-xs">Your writes in this tab</span>
			</div>
			<HoldToConfirm
				label="Clear"
				armedLabel="Press again"
				confirmedLabel="Cleared"
				variant="destructive"
				size="xs"
				class="ml-auto"
				duration={900}
				disabled={entries.length === 0 && !cleared}
				bind:confirmed={cleared}
				onConfirm={() => onclear?.()}
			/>
		</div>
		<div class="max-h-[60svh] overflow-y-auto" data-slot="activity-list">
			{#if entries.length === 0}
				<p class="text-muted-foreground px-4 py-8 text-center text-sm">
					Nothing yet. What you change here is listed with its outcome.
				</p>
			{:else}
				{#each groups as group (group.key)}
					<section aria-label={group.heading}>
						<h3 class="text-muted-foreground bg-muted px-4 py-1.5 text-[0.6875rem] uppercase">
							{group.heading}
						</h3>
						<ul class="divide-border/60 divide-y">
							{#each group.entries as entry (entry.id)}
								<li class="flex items-start gap-3 px-4 py-2.5" data-slot="activity-entry">
									{#if entry.ok}
										<CircleCheck class="text-success mt-0.5 size-4 shrink-0" aria-hidden="true" />
									{:else}
										<CircleX class="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
									{/if}
									<div class="min-w-0 flex-1">
										<p class="text-sm break-words">{entry.label}</p>
										<time
											class="text-muted-foreground block text-xs tabular-nums"
											datetime={new Date(entry.at).toISOString()}
										>
											{formatZoned(entry.at)}
										</time>
										{#if entry.ok}
											<p class="text-success text-xs">Succeeded</p>
										{:else}
											<p class="text-destructive text-xs">{failureLabel(entry.status)}</p>
											{#if entry.detail}
												<p class="text-muted-foreground mt-0.5 text-xs break-words">
													{entry.detail}
												</p>
											{/if}
										{/if}
									</div>
								</li>
							{/each}
						</ul>
					</section>
				{/each}
			{/if}
		</div>
	</Popover.Content>
</Popover.Root>
