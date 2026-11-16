<script lang="ts">
	/**
	 * The dataset page's dock: `horizon-layout` over the workbench's panels, with the controls the library
	 * leaves to its host — close a tab, open a closed panel, maximize and restore, pop out.
	 *
	 * Every panel the workbench has is reached from the one "open a panel" menu on each group; there is
	 * no other navigation between a dataset's views.
	 *
	 * The whole arrangement is the one bound `layout` value; every control here writes a new value
	 * through `layout.ts`, and the library validates each against `views` before it draws it.
	 *
	 * THE PANEL RULE: the library renders only the active tab of a group and remounts a panel when it
	 * is moved, maximized or popped out, so a panel holds nothing in component state that must survive
	 * that. Its data lives in a module or a shared read instance (`#lib/explore/explore.svelte.ts`, the shared reads of `#lib/api`);
	 * scroll position is lost and that is accepted.
	 */
	import { HorizonLayout, type Id, type LayoutConfig, type View } from 'horizon-layout';
	import { onMount } from 'svelte';
	import { SvelteMap } from 'svelte/reactivity';
	import { Check, ExternalLink, Maximize2, Minimize2, Plus, X } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';
	import * as DropdownMenu from '@workbench/ui/dropdown-menu';
	import { GatedAction } from '@workbench/ui/gated-action';
	import type { PanelGates } from '#lib/explore/pane-capabilities.ts';
	import { dockControl } from './dock-control.svelte.ts';
	import AtlasPanel from './panels/AtlasPanel.svelte';
	import GraphPanel from './panels/GraphPanel.svelte';
	import PlayerPanel from './panels/PlayerPanel.svelte';
	import ResultsPanel from './panels/ResultsPanel.svelte';
	import RowPanel from './panels/RowPanel.svelte';
	import RowsPanel from './panels/RowsPanel.svelte';
	import TreePanel from './panels/TreePanel.svelte';
	import UnavailablePanel from './panels/UnavailablePanel.svelte';
	import {
		PANELS,
		PLAIN_TITLES,
		closePanel,
		closedPanels,
		isPanelId,
		maximizePanel,
		openPanel,
		popOut,
		revealPanel,
		shownPanels,
		type PanelId,
	} from './layout.ts';

	/**
	 * `plain`: the dataset carries no workbench descriptor, so its results and player are the plain
	 * table's rows and the row opened (`#lib/plain/plain.svelte.ts`). `gates`: which panels the dataset can
	 * feed (`pane-capabilities`); one it cannot is listed in the menu disabled with its reason, and shows
	 * that reason inside where a saved layout or a link opened it.
	 */
	let {
		layout = $bindable(),
		plain = false,
		gates,
	}: { layout: LayoutConfig; plain?: boolean; gates: PanelGates } = $props();

	const titleOf = (id: Id): string =>
		(plain ? PLAIN_TITLES[id] : undefined) ?? PANELS.find((p) => p.id === id)?.title ?? id;

	/**
	 * A popped-out panel is mounted in another document, which carries the page's stylesheets but
	 * not its theme class: the page's `<html>` classes and `color-scheme` are mirrored onto it while
	 * the panel lives there.
	 */
	function followTheme(node: HTMLElement): (() => void) | undefined {
		const own = node.ownerDocument.documentElement;
		const page = document.documentElement;
		if (own === page) return undefined;
		const copy = (): void => {
			own.className = page.className;
			own.style.colorScheme = page.style.colorScheme;
		};
		copy();
		const watch = new MutationObserver(copy);
		watch.observe(page, { attributes: true, attributeFilter: ['class', 'style'] });
		return () => watch.disconnect();
	}

	const closed = $derived(closedPanels(layout));
	/** Unique per dock: two docks (wide and narrow) never share a reason's id. */
	const menuId = $props.id();
	const maximized = $derived(layout.maximizedView);

	function close(id: Id): void {
		layout = closePanel(layout, id);
	}

	function openPanelBeside(id: PanelId, beside?: Id): void {
		layout = openPanel(layout, id, beside);
	}

	function maximize(id: Id): void {
		layout = maximizePanel(layout, id);
	}

	/** Bring an open panel in front, from the menu. */
	function reveal(id: PanelId): void {
		layout = revealPanel(layout, id);
	}

	const shown = $derived(new Set(shownPanels(layout)));

	// The panels ask the dock to bring another in front (`dock-control.svelte.ts`) while it is mounted.
	onMount(() =>
		dockControl.attach({
			reveal: (id) => (layout = revealPanel(layout, id)),
			maximized: () => layout.maximizedView,
		}),
	);

	function restore(): void {
		const next = { ...layout };
		delete next.maximizedView;
		layout = next;
	}

	// Top-level snippets, so the script can hand them to the library. One map for the dock's life:
	// the library re-validates the layout whenever `views` changes.
	const views = new SvelteMap<Id, View>([
		['results', { title: titleOf('results'), snippet: results, tabControls: [closeTab] }],
		['player', { title: titleOf('player'), snippet: player, tabControls: [closeTab] }],
		['atlas', { title: titleOf('atlas'), snippet: atlas, tabControls: [closeTab] }],
		['graph', { title: titleOf('graph'), snippet: graph, tabControls: [closeTab] }],
		['tree', { title: titleOf('tree'), snippet: tree, tabControls: [closeTab] }],
	]);

	/** A closed popout window returns its panel to the dock. The library has already dropped it from
	 *  `popouts`; closing it first keeps this correct whichever value the binding holds now. */
	function returnFromPopout(id: Id): void {
		if (isPanelId(id)) layout = openPanel(closePanel(layout, id), id);
	}
</script>

{#snippet results()}
	<div class="dock-panel" {@attach followTheme}>
		{#if plain}<RowsPanel />{:else}<ResultsPanel />{/if}
	</div>
{/snippet}
{#snippet player()}
	<div class="dock-panel" {@attach followTheme}>
		{#if plain}<RowPanel />{:else}<PlayerPanel />{/if}
	</div>
{/snippet}
{#snippet atlas()}
	<div class="dock-panel" {@attach followTheme}>
		{#if gates.atlas !== null}
			<UnavailablePanel title={titleOf('atlas')} reason={gates.atlas} />
		{:else}
			<AtlasPanel />
		{/if}
	</div>
{/snippet}
{#snippet graph()}
	<div class="dock-panel" {@attach followTheme}>
		{#if gates.graph !== null}
			<UnavailablePanel title={titleOf('graph')} reason={gates.graph} />
		{:else}
			<GraphPanel />
		{/if}
	</div>
{/snippet}
{#snippet tree()}
	<div class="dock-panel" {@attach followTheme}>
		{#if gates.tree !== null}
			<UnavailablePanel title={titleOf('tree')} reason={gates.tree} />
		{:else}
			<TreePanel />
		{/if}
	</div>
{/snippet}

{#snippet closeTab(id: Id)}
	<button
		type="button"
		class="dock-tab-close"
		aria-label="Close {titleOf(id)}"
		title="Close {titleOf(id)}"
		onclick={(e) => {
			e.stopPropagation();
			close(id);
		}}
	>
		<X class="size-3" />
	</button>
{/snippet}

{#snippet panelPicker(beside?: Id)}
	<!-- Every panel is listed: an open one is brought in front, a closed one opens beside the panel whose
	     control was used, and one the dataset cannot feed is listed disabled with its reason. -->
	<DropdownMenu.Root>
		<DropdownMenu.Trigger>
			{#snippet child({ props })}
				<Button
					{...props}
					variant="ghost"
					size="icon-xs"
					aria-label="Open a panel"
					title="Open a panel"
				>
					<Plus />
				</Button>
			{/snippet}
		</DropdownMenu.Trigger>
		<DropdownMenu.Content class="w-64" align="end">
			<DropdownMenu.Label class="text-muted-foreground text-xs">Open a panel</DropdownMenu.Label>
			{#each PANELS as panel (panel.id)}
				{@const why = gates[panel.id]}
				{@const open = !closed.includes(panel.id)}
				{#if why === null}
					<DropdownMenu.Item
						onSelect={() => (open ? reveal(panel.id) : openPanelBeside(panel.id, beside))}
					>
						<span class="flex-1">{titleOf(panel.id)}</span>
						{#if open}
							<span class="text-muted-foreground flex items-center gap-1 text-xs"
								><Check class="size-3" />{shown.has(panel.id) ? 'shown' : 'open'}</span
							>
						{/if}
					</DropdownMenu.Item>
				{:else}
					<!-- One menu item, reached by the arrows like the rest, that does nothing when chosen: its name
					     is the panel's, its description the reason, shown under the name and announced once. Its own
					     element (`child`), so `aria-disabled` is set after the menu's props rather than under them. -->
					<DropdownMenu.Item closeOnSelect={false} onSelect={(event) => event.preventDefault()}>
						{#snippet child({ props })}
							<div
								{...props}
								aria-label={titleOf(panel.id)}
								aria-disabled="true"
								aria-describedby="why-{menuId}-{panel.id}"
								class="data-[highlighted]:bg-accent flex w-full cursor-not-allowed flex-col items-start rounded-sm px-2 py-1.5 text-sm outline-none select-none"
							>
								<span class="text-muted-foreground">{titleOf(panel.id)}</span>
								<span class="text-foreground text-xs leading-snug"
									>Not available: <span id="why-{menuId}-{panel.id}">{why}</span></span
								>
							</div>
						{/snippet}
					</DropdownMenu.Item>
				{/if}
			{/each}
		</DropdownMenu.Content>
	</DropdownMenu.Root>
{/snippet}

{#snippet groupActions(active: Id)}
	<div class="flex items-center gap-0.5">
		{@render panelPicker(active)}
		<Button
			variant="ghost"
			size="icon-xs"
			aria-label="Maximize {titleOf(active)}"
			title="Maximize {titleOf(active)}"
			onclick={() => maximize(active)}
		>
			<Maximize2 />
		</Button>
		<Button
			variant="ghost"
			size="icon-xs"
			aria-label="Pop out {titleOf(active)}"
			title="Pop out {titleOf(active)} into its own window"
			onclick={() => (layout = popOut(layout, active))}
		>
			<ExternalLink />
		</Button>
	</div>
{/snippet}

{#if maximized}
	<div class="dock-maximized-bar">
		<span class="text-sm font-medium">{titleOf(maximized)}</span>
		<Button variant="ghost" size="xs" onclick={restore}>
			<Minimize2 /> Restore
		</Button>
	</div>
{/if}

<div class="dock-frame">
	<!-- Mounted whenever the dock is: the library owns the popout windows and closes every one of them
	     when it unmounts, so an empty dock with panels in their own windows must keep it. -->
	<HorizonLayout
		bind:config={layout}
		{views}
		tabgroupControls={[groupActions]}
		onPopoutClose={returnFromPopout}
	/>
	{#if !layout.root && !maximized}
		<div class="dock-empty">
			<p class="text-muted-foreground text-sm">
				{layout.popouts?.length ? 'Every open panel is in its own window.' : 'No panel is open.'}
			</p>
			<div class="flex gap-2">
				{#each closed as id (id)}
					<GatedAction
						allowed={gates[id] === null}
						action="Open {titleOf(id)}"
						reason={gates[id] ?? ''}
					>
						<Button variant="outline" size="sm" onclick={() => openPanelBeside(id)}>
							<Plus />
							{titleOf(id)}
						</Button>
					</GatedAction>
				{/each}
			</div>
		</div>
	{/if}
</div>

<style>
	.dock-frame {
		position: relative;
		display: flex;
		flex: 1 1 0;
		min-height: 0;
		min-width: 0;
	}
	.dock-panel {
		height: 100%;
		width: 100%;
		overflow: auto;
	}
	.dock-maximized-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.25rem 0.5rem;
		border-bottom: 1px solid var(--border);
		background: var(--background);
	}
	/* Over the empty layout, which stays mounted for its popout windows. */
	.dock-empty {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 0.75rem;
	}
	.dock-tab-close {
		display: inline-grid;
		place-items: center;
		width: 1rem;
		height: 1rem;
		border: none;
		border-radius: calc(var(--radius) - 6px);
		background: transparent;
		color: inherit;
		opacity: 0.6;
		cursor: pointer;
	}
	.dock-tab-close:hover,
	.dock-tab-close:focus-visible {
		opacity: 1;
		background: var(--accent);
	}
</style>
