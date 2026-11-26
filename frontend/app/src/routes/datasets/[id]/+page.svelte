<script lang="ts">
	/**
	 * One dataset: a dock of the workbench's panels over it (Results, Player, Atlas, Graph, Tree), the
	 * page's one navigation. The arrangement is the dataset's: restored once, autosaved after every change,
	 * and can be saved as a named view with a plain table's set-ups and the rows it listed; where both are
	 * kept is `#lib/dock/store.ts`'s decision alone (this browser, per person). A dataset opened with no
	 * link that says otherwise opens with its default view, else its last arrangement, else the default
	 * that fits it (`defaultLayout`): only the panels it can feed (`panelGates`).
	 *
	 * Below a tablet's width the dock shows the stacked arrangement instead (`defaultLayout(true)`), from
	 * the moment the window crosses the breakpoint, whichever way: the person's own arrangement is kept,
	 * saved and shown again once the window is wide, and the stacked one is never saved over it.
	 *
	 * THE ADDRESS IS THE VIEW (`#lib/dock/link.ts`): the search, the topic, the row opened, the graph's
	 * entity, a plain table's set-ups and the node its rows are filtered to, the panels shown and the one
	 * maximized are written into it as they change (replacing the
	 * history entry, so Back leaves the dataset), and a link or a reload opens on them: the panels it names
	 * are brought in front over the restored layout.
	 *
	 * A SHARED VIEW'S LINK (`?view=<id>`, ADR 0022) opens the view the annotator keeps under that id, read under the
	 * reader's own rights, and applies it over the dataset's default view: the same arrangement, table, set-ups and
	 * search the sharer saved, checked part by part as a view kept in this browser is. Once applied the parameter
	 * leaves the address, which then carries what is shown as any address here does. A refusal is said with the door's
	 * reason and nothing is applied.
	 *
	 * WHAT A VIEW NAMES THAT IS GONE (a table the dataset no longer holds, a column its table no longer has) is left out
	 * and named in the dock's one status line (`#lib/dock/view-fit.ts`), for a view kept here and a shared one alike.
	 *
	 * The page also hosts what every panel shares and none owns: the health poll the search modes read,
	 * and the keyboard's `/` and Escape.
	 */
	import { onMount, tick, untrack } from 'svelte';
	import { MediaQuery } from 'svelte/reactivity';
	import type { LayoutConfig } from 'horizon-layout';
	import { navigating, page } from '$app/state';
	import { resolve } from '$app/paths';
	import { afterNavigate, goto } from '$app/navigation';
	import { Button } from '@riksarkivet/ui/button';
	import { auth } from '#lib/auth.svelte.ts';
	import Dock from '#lib/dock/Dock.svelte';
	import { dockControl } from '#lib/dock/dock-control.svelte.ts';
	import ViewSidebar from '#lib/dock/ViewSidebar.svelte';
	import {
		defaultLayout,
		isPanelId,
		maximizePanel,
		revealPanel,
		shownPanels,
		type LayoutFit,
	} from '#lib/dock/layout.ts';
	import { followLink } from '#lib/dock/follow-link.ts';
	import { readDockLink, writeDockLink, type DockLink } from '#lib/dock/link.ts';
	import { startWorkbench, type WorkbenchSession } from '#lib/dock/session.ts';
	import { openWorkbenchStore, type ViewSnapshot } from '#lib/dock/store.ts';
	import { fitView } from '#lib/dock/view-fit.ts';
	import { readSharedView, type ViewRead } from '#lib/dock/views.svelte.ts';
	import type { ApiResult } from '#lib/api/client.ts';
	import { openSharedView, sharedViewsOf, type SharedView } from '#lib/api/views.api.ts';
	import { told } from '#lib/projects/event-words.ts';
	import { plain } from '#lib/plain/plain.svelte.ts';
	import { explore } from '#lib/explore/explore.svelte.ts';
	import { panelGates } from '#lib/explore/pane-capabilities.ts';
	import { serviceHealth } from '#lib/explore/service-health.svelte.ts';
	import { graphExplorer } from '#lib/graph/graph-explorer.svelte.ts';
	import { titleOf } from '#lib/nav.ts';

	let { data } = $props();
	// The layout mounts this page afresh for each dataset: the id, the dataset's shape and its gates are
	// read once.
	// svelte-ignore state_referenced_locally
	const id = data.id;
	/** A dataset with no workbench descriptor: the dock holds its plain table (`#lib/plain`). */
	// svelte-ignore state_referenced_locally
	const isPlain = data.facts !== null && !data.facts.stamped;
	// svelte-ignore state_referenced_locally
	const view = data.view;
	const gates = $derived(
		panelGates(view, {
			rowTable: view?.searchTable(page.url.searchParams.get('table'))?.row_table ?? null,
			plain: isPlain,
			columns: isPlain && plain.table ? (plain.facts?.tables[plain.table]?.columns ?? []) : [],
			...(plain.facts ? { limits: plain.facts.setup_limits } : {}),
		}),
	);
	/** What the default layout is fitted to: the dataset's shape and the panels it can feed. */
	const fit = (): LayoutFit => ({
		kind: isPlain ? 'plain' : view?.isPlayable && view.hasTime ? 'time' : 'image',
		gates,
	});
	/**
	 * The link this page opened on. Back or Forward to an entry whose address this page wrote (a shallow entry)
	 * loads the page at the address it first opened at (`page.url`, which the load followed); the entry's own
	 * address is `page.shallow.url`, and that is the view to show: it is followed here, over the load's.
	 */
	const opened = untrack((): DockLink => {
		const entry = page.shallow?.url;
		return entry && view && entry.search !== page.url.search
			? followLink(view, entry.searchParams)
			: data.link;
	});

	/** `base` with the panels a link named brought in front, and the one it maximized maximized. */
	function withLink(base: LayoutConfig, link: DockLink = opened): LayoutConfig {
		let next = base;
		for (const panel of link.panels) next = revealPanel(next, panel);
		return link.max === null ? next : maximizePanel(next, link.max);
	}

	let layout = $state<LayoutConfig | null>(null);
	/** Whether the window is below a tablet's width, followed as it is resized. */
	const narrow = new MediaQuery('width < 768px');
	/** What the dock shows while narrow: the stacked arrangement, its edits kept for the page's life only. */
	let narrowLayout = $state<LayoutConfig>(withLink(defaultLayout(true, fit())));
	let session = $state.raw<WorkbenchSession | null>(null);
	/** Why the stored layout could not be read; while set, nothing is saved over it. */
	let unreadable = $state<string | null>(null);
	/** The layout as last restored or saved, so the restored value is not written back. */
	let last: string | null = null;
	/** Why the link this page opened on carried a set-up that was not used. */
	// svelte-ignore state_referenced_locally
	let linkProblem = $state<string | null>(data.link.setupProblem ?? null);
	/** The shared view this page's link names, read once as the page opens. */
	const sharedId = untrack(() => page.url.searchParams.get('view'));
	/** Whether that shared view was applied, after which its parameter leaves the address. */
	let sharedApplied = $state(false);
	/** What applying a view did that the person should know: a shared view opened, a refusal, or what is gone. */
	let viewNotice = $state<{ text: string; problem: boolean } | null>(null);

	/** What a view holds of what is on screen now. */
	function snapshot(): ViewSnapshot | null {
		if (layout === null) return null;
		return {
			layout: $state.snapshot(layout),
			table: isPlain ? plain.table : null,
			setups: isPlain ? $state.snapshot(plain.setups) : {},
			q: isPlain ? plain.rows.q : explore.spec.q || null,
			node: isPlain && plain.node ? { of: plain.node.of, id: plain.node.key } : null,
		};
	}

	/** Whether the link this page opened on says what to show, over the dataset's default view. */
	const linked = (link: DockLink): boolean =>
		link.panels.length > 0 ||
		link.max !== null ||
		Object.keys(link.setups).length > 0 ||
		link.node !== null ||
		link.q !== null ||
		link.topic !== null ||
		link.row !== null ||
		link.entity !== null;

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
			event.preventDefault();
			void focusDatasetSearch();
		} else if (event.key === 'Escape' && explore.active) {
			explore.active = null;
		}
	}

	/** The dataset's own search box (`data-dataset-search`), never another panel's: the Results (or Rows)
	 *  panel is brought in front first when it is closed or behind a tab. */
	async function focusDatasetSearch(): Promise<void> {
		const find = (): HTMLInputElement | null =>
			document.querySelector<HTMLInputElement>('input[data-dataset-search]');
		if (!find()) {
			dockControl.reveal('results');
			await tick();
		}
		const box = find();
		box?.focus();
		box?.select();
	}

	onMount(() => serviceHealth.subscribe());

	onMount(() => {
		let left = false;
		const opening = openWorkbenchStore(auth.enabled ? (auth.session?.sub ?? null) : null);
		// Asked for beside the store, and applied once the dock is up.
		const sharedOpening = sharedId === null ? null : openSharedView(sharedId);
		void startWorkbench(
			opening,
			id,
			{
				snapshot,
				shared: sharedViewsOf(id),
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
					layout = withLink(read.status === 'ok' ? read.layout : firstLayout());
					session = started;
					if (sharedOpening === null) applyDefault();
					void sharedOpening?.then((result) => {
						if (!left) openShared(result);
					});
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

	/** Apply a saved view; one this app cannot show leaves what is on screen as it is, and says why. */
	function applyView(viewId: string): void {
		const read: ViewRead | undefined = session?.views.select(viewId);
		if (read === undefined) return;
		if (read.status === 'unreadable') {
			session?.views.report(`This view could not be applied: ${read.detail}.`);
			return;
		}
		if (read.status !== 'ok') return;
		const missing = applySnapshot(read.view);
		viewNotice = missing.length > 0 ? { text: missing.join(' '), problem: true } : null;
		session?.views.activate(viewId);
	}

	/** Each table the plain dataset holds, by name, and its columns. */
	function heldTables(): Record<string, string[]> {
		return Object.fromEntries(
			Object.entries(plain.facts?.tables ?? {}).map(([name, table]) => [
				name,
				table.columns.map((c) => c.name),
			]),
		);
	}

	/** Apply what a view holds that the dataset still holds; answers what it names that is gone, as sentences. */
	function applySnapshot(saved: ViewSnapshot): readonly string[] {
		const { view: held, missing } = isPlain
			? fitView(saved, heldTables(), plain.facts?.unreadable ?? {})
			: { view: saved, missing: [] };
		if (isPlain) {
			// A table that is gone takes its set-ups, node and search with it: the table in view keeps its own.
			if (saved.table === null || held.table !== null) {
				if (held.table !== null && held.table !== plain.table) plain.choose(held.table);
				plain.restore(held.setups, held.node);
				if (held.q) plain.search(held.q);
				else if (held.node === null && plain.rows.q) plain.reset();
			}
		} else if (held.q && held.q !== explore.spec.q) {
			explore.apply({ ...explore.spec, q: held.q });
		}
		layout = held.layout;
		return missing;
	}

	/** The dataset's default view, where it has one and the link names nothing else to show. */
	function applyDefault(): void {
		const preferred = session?.views.defaultId ?? null;
		if (preferred !== null && !linked(opened)) applyView(preferred);
	}

	/**
	 * Apply the shared view this page's link names, or say why it was not and open as the dataset opens with no shared
	 * view. It is not made the sidebar's active view: only its owner's listing could show it, and that listing is read
	 * beside it, so the opened view is told in the status line instead, the same for every reader on every load.
	 */
	function openShared(result: ApiResult<SharedView>): void {
		const refuse = (text: string): void => {
			applyDefault();
			viewNotice = { text, problem: true };
		};
		if (!result.ok) {
			refuse(`This shared view could not be opened: ${result.detail}`);
			return;
		}
		const shared = result.data;
		if (shared.dataset !== id) {
			refuse(
				`This shared view is of the dataset ${shared.dataset}, not this one: it was not applied.`,
			);
			return;
		}
		const read = readSharedView(shared);
		if (read.status !== 'ok') {
			refuse(
				`The shared view ${shared.name} could not be applied: ${read.status === 'unreadable' ? read.detail : 'it holds nothing'}.`,
			);
			return;
		}
		const missing = applySnapshot(read.view);
		sharedApplied = true;
		viewNotice = {
			text: [told('Opened the shared view', shared.name), ...missing].join(' '),
			problem: missing.length > 0,
		};
	}

	/** The first visit's arrangement: the wide one, which a narrow window shows stacked meanwhile. */
	const firstLayout = (): LayoutConfig => defaultLayout(false, fit());

	// A link to this same dataset (an in-app link, or Back and Forward to such a link's entry) does not remount
	// the page: the panels it names are brought in front once it has landed. The page's own address writes are
	// shallow `goto`s of what it already shows, and are not followed: one that lands after the layout has
	// changed again would bring back what the person just put away.
	afterNavigate((navigation) => {
		const to = navigation.to?.url;
		if (navigation.type !== 'link' && navigation.type !== 'popstate') return;
		if (!to || navigation.from === null || to.pathname !== location.pathname) return;
		const link = readDockLink(to.searchParams);
		if (layout !== null) layout = withLink(layout, link);
		narrowLayout = withLink(narrowLayout, link);
	});

	// The address follows the view: written once the layout is restored, so the link that opened the page
	// is read before anything is written over it. An effect, since the address is outside Svelte's state.
	// Written only while this page's dataset is the one the shared state holds and no navigation is under
	// way: another dataset's load fills that state before this page unmounts, and writing then would put
	// that dataset's view into this page's history entry.
	$effect(() => {
		const shown = narrow.current ? narrowLayout : layout;
		if (shown === null || navigating.type !== null) return;
		if (explore.dataset !== id || (!isPlain && graphExplorer.dataset !== id)) return;
		if (isPlain && plain.facts?.id !== id) return;
		const active = explore.active;
		/** A row a link opened that still waits for its search keeps its place in the address. */
		const linked = active === null ? explore.linked : null;
		const time = active && view?.hasTime ? view.time(active) : null;
		const searched = !isPlain && explore.mapSelection === null;
		// The address as it is now (a shallow replace is not a navigation), not as the page opened.
		const here = new URLSearchParams(location.search);
		if (sharedApplied) here.delete('view');
		if (isPlain && plain.table !== null && Object.keys(plain.facts?.tables ?? {}).length > 1) {
			here.set('table', plain.table);
		}
		const params = writeDockLink(here, {
			setups: isPlain ? plain.setups : {},
			node: isPlain && plain.node ? { of: plain.node.of, id: plain.node.key } : null,
			q: isPlain ? plain.rows.q : searched && explore.spec.q ? explore.spec.q : null,
			mode: searched && explore.spec.q ? (explore.spec.mode ?? null) : null,
			topic: searched ? (explore.spec.topic ?? null) : null,
			row:
				active && view
					? view.keyFields.map((k) => String(active[k] ?? ''))
					: linked
						? [...linked.keys]
						: null,
			doc: null,
			t: time ? time.start : (linked?.t ?? null),
			entity: isPlain ? null : graphExplorer.selectedId,
			panels: shownPanels(shown).filter(isPanelId),
			max:
				shown.maximizedView !== undefined && isPanelId(shown.maximizedView)
					? shown.maximizedView
					: null,
		});
		const query = params.toString();
		if (query === new URLSearchParams(location.search).toString()) return;
		// The route id is resolved; the query the rule cannot see follows it. A shallow replace: no load runs,
		// and Back still leaves the dataset.
		const href = `${resolve('/datasets/[id]', { id: encodeURIComponent(id) })}?${query}${location.hash}`;
		// oxlint-disable-next-line svelte/no-navigation-without-resolve
		goto(href, { state: page.state, shallow: true, replace: true }).catch(() => {
			// Refused only when another navigation overtook it, whose page writes its own address.
		});
	});

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
		<ViewSidebar {views} dataset={id} onselect={applyView} />
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
		{:else if viewNotice}
			<p class={['note', { problem: viewNotice.problem }]} role="status">
				<span>{viewNotice.text}</span>
				<Button variant="ghost" size="xs" onclick={() => (viewNotice = null)}>Dismiss</Button>
			</p>
		{:else if linkProblem}
			<p class="note problem" role="status">
				<span>This link's set-up was not used: {linkProblem}.</span>
				<Button variant="ghost" size="xs" onclick={() => (linkProblem = null)}>Dismiss</Button>
			</p>
		{:else if store && !store.persistent}
			<p class="note" role="status">
				This browser keeps no storage for this site: the layout and views last until the tab closes.
			</p>
		{/if}
		{#if layout}
			{#if narrow.current}
				<Dock bind:layout={narrowLayout} plain={isPlain} {gates} />
			{:else}
				<Dock bind:layout plain={isPlain} {gates} />
			{/if}
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
