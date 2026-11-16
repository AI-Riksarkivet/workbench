<script lang="ts">
	/**
	 * One dataset: a dock of the workbench's panels over it (Results, Player, Atlas). The arrangement is
	 * restored once, autosaved after every change, and can be saved as a named view; where both are kept
	 * is `#lib/dock/store.ts`'s decision alone (this browser, per person).
	 *
	 * Below a tablet's width the dock shows the stacked arrangement instead (`defaultLayout(true)`), from
	 * the moment the window crosses the breakpoint, whichever way: the person's own arrangement is kept,
	 * saved and shown again once the window is wide, and the stacked one is never saved over it.
	 *
	 * The page also hosts what every panel shares and none owns: the health poll the search modes read,
	 * and the keyboard's `/` and Escape.
	 */
	import { onMount } from 'svelte';
	import { MediaQuery } from 'svelte/reactivity';
	import type { LayoutConfig } from 'horizon-layout';
	import { Button } from '@workbench/ui/button';
	import { auth } from '#lib/auth.svelte.ts';
	import Dock from '#lib/dock/Dock.svelte';
	import ViewSidebar from '#lib/dock/ViewSidebar.svelte';
	import { defaultLayout } from '#lib/dock/layout.ts';
	import { startWorkbench, type WorkbenchSession } from '#lib/dock/session.ts';
	import { openWorkbenchStore } from '#lib/dock/store.ts';
	import { explore } from '#lib/explore/explore.svelte.ts';
	import { serviceHealth } from '#lib/explore/service-health.svelte.ts';
	import { titleOf } from '#lib/nav.ts';

	let { data } = $props();
	const id = $derived(data.id);
	/** A dataset with no workbench descriptor: the dock holds its plain table (`#lib/plain`). */
	const isPlain = $derived(data.facts !== null && !data.facts.stamped);

	let layout = $state<LayoutConfig | null>(null);
	/** Whether the window is below a tablet's width, followed as it is resized. */
	const narrow = new MediaQuery('width < 768px');
	/** What the dock shows while narrow: the stacked arrangement, its edits kept for the page's life only. */
	let narrowLayout = $state<LayoutConfig>(defaultLayout(true));
	let session = $state.raw<WorkbenchSession | null>(null);
	/** Why the stored layout could not be read; while set, nothing is saved over it. */
	let unreadable = $state<string | null>(null);
	/** The layout as last restored or saved, so the restored value is not written back. */
	let last: string | null = null;

	const store = $derived(session?.store ?? null);
	const autosave = $derived(session?.autosave ?? null);
	const views = $derived(session?.views ?? null);

	/** `/` focuses the search box; Escape closes the open item. Neither while typing in a field. */
	function onKeydown(event: KeyboardEvent): void {
		const target = event.target as HTMLElement | null;
		const typing =
			!!target &&
			(target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable);
		if (typing) return;
		if (event.key === '/') {
			const box = document.querySelector<HTMLInputElement>('input[type="search"]');
			if (!box) return;
			event.preventDefault();
			box.focus();
			box.select();
		} else if (event.key === 'Escape' && explore.active) {
			explore.active = null;
		}
	}

	onMount(() => serviceHealth.subscribe());

	onMount(() => {
		let left = false;
		const opening = openWorkbenchStore(auth.enabled ? (auth.session?.sub ?? null) : null);
		void startWorkbench(
			opening,
			{
				snapshot: () => (layout ? $state.snapshot(layout) : null),
				onLayout: (read, wasRefused) => {
					unreadable = read.status === 'unreadable' ? read.detail : null;
					if (wasRefused && read.status === 'ok') {
						last = JSON.stringify(read.layout);
						layout = read.layout;
					}
				},
				started: (started) => {
					const read = started.restored;
					unreadable = read.status === 'unreadable' ? read.detail : null;
					layout = read.status === 'ok' ? read.layout : firstLayout();
					session = started;
				},
			},
			() => left,
		);
		return () => {
			left = true;
			session?.stop();
		};
	});

	// Every change after the restore: the named view re-checks its modified marker, and the draft is
	// saved. The restored value itself is not written back.
	$effect(() => {
		if (layout === null || autosave === null) return;
		const snapshot = $state.snapshot(layout);
		const serialized = JSON.stringify(snapshot);
		if (last === null || serialized === last) {
			last = serialized;
			return;
		}
		last = serialized;
		views?.touch();
		autosave.schedule(snapshot);
	});

	/** Apply a saved view; one this app cannot show leaves the arrangement as it is, and says why. */
	function applyView(viewId: string): void {
		const read = views?.select(viewId);
		if (read === undefined) return;
		if (read.status === 'unreadable') {
			views?.report(`This view could not be applied: ${read.detail}.`);
			return;
		}
		if (read.status !== 'ok') return;
		layout = read.layout;
		views?.activate(viewId);
	}

	/** The first visit's arrangement: the wide one, which a narrow window shows stacked meanwhile. */
	const firstLayout = (): LayoutConfig => defaultLayout();

	/** The person's explicit choice to replace a stored layout that could not be read. */
	function resetLayout(): void {
		const fresh = firstLayout();
		autosave?.reset(fresh);
		unreadable = null;
		last = JSON.stringify(fresh);
		layout = fresh;
		void views?.refresh();
	}
</script>

<svelte:head><title>{titleOf(id, 'Datasets')}</title></svelte:head>
<!-- Leaving or reloading the page writes a save the debounce still holds. -->
<svelte:window onpagehide={() => autosave?.flush()} onkeydown={onKeydown} />

<!-- The page's heading names the dataset; the dock's panels head themselves one level under it. -->
<h1 class="sr-only">{id}</h1>
<div class="wrap">
	{#if views}
		<ViewSidebar {views} onselect={applyView} />
	{/if}
	<div class="dock">
		{#if unreadable}
			<p class="note problem" role="status">
				<span>
					Your saved layout could not be read ({unreadable}). It has not been changed; the default
					is shown and changes are not saved.
				</span>
				<Button variant="outline" size="xs" onclick={resetLayout}>Reset layout</Button>
			</p>
		{:else if store && !store.persistent}
			<p class="note" role="status">
				This browser keeps no storage for this site: the layout and views last until the tab closes.
			</p>
		{/if}
		{#if layout}
			<!-- Each dataset mounts its panels afresh: they read the dataset's view when they mount. -->
			{#key id}
				{#if narrow.current}
					<Dock bind:layout={narrowLayout} plain={isPlain} />
				{:else}
					<Dock bind:layout plain={isPlain} />
				{/if}
			{/key}
		{/if}
	</div>
</div>

<style>
	.wrap {
		display: flex;
		flex: 1 1 0;
		min-height: 0;
		width: 100%;
	}
	/* Below a tablet's width the views rail moves above the dock, so the panels keep the full width. */
	@media (width < 768px) {
		.wrap {
			flex-direction: column;
		}
	}
	/* The dock fills a sized flex item; `min-height: 0` lets it shrink below its content. The stacking
	   context keeps anything the dock positions from painting over the shell's dropdowns. */
	.dock {
		display: flex;
		flex: 1 1 0;
		flex-direction: column;
		min-height: 0;
		min-width: 0;
		position: relative;
		z-index: 0;
		isolation: isolate;
	}
	.note {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		margin: 0;
		padding: 0.375rem 0.75rem;
		border-bottom: 1px solid var(--border);
		color: var(--muted-foreground);
		font-size: 0.75rem;
	}
	.note.problem {
		color: var(--destructive);
	}
</style>
