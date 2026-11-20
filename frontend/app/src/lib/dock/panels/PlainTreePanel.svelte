<script module lang="ts">
	/** The treemap's layout and drill, kept across the dock remounting the panel, and started afresh for
	 *  another set-up. */
	const kept = $state<{ key: string; view: 'drill' | 'nested' | 'flow'; drill: string[] }>({
		key: '',
		view: 'drill',
		drill: [],
	});
</script>

<script lang="ts">
	/**
	 * The Tree of a table nobody stamped, from the columns a person picked (`#lib/plain/setup.ts`): each row
	 * under the row its parent column names, or at the place its path column spells. Until it is set up, the
	 * panel is its set-up form, which shows the tree's orphans, cycles and size before it is drawn.
	 *
	 * The hierarchy is drawn by the stamped tree's treemap (`#lib/topics/topic-treemap.svelte`), its largest
	 * branches kept and the rest folded into "more"; choosing a node lists, in the Rows panel, the rows at or
	 * below it (its own rows and every descendant's, the rows its size counts), and choosing its
	 * "(itself)" leaf the rows at it alone, read from the node's own door by its key.
	 */
	import { Loader2, Settings2, X } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { derivedTree, tableProfile } from '#lib/api/derived.api.ts';
	import { dockControl } from '#lib/dock/dock-control.svelte.ts';
	import { failureText } from '#lib/failure.ts';
	import NoTable from '#lib/plain/NoTable.svelte';
	import SetupForm from '#lib/plain/SetupForm.svelte';
	import { extentNote } from '#lib/plain/extent.ts';
	import { plain } from '#lib/plain/plain.svelte.ts';
	import { missingColumns, type TreeSetup } from '#lib/plain/setup.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import TopicTreemap from '#lib/topics/topic-treemap.svelte';
	import RowsPanel from './RowsPanel.svelte';

	const facts = $derived(plain.facts);
	const table = $derived(plain.table);
	const info = $derived(facts && table ? (facts.tables[table] ?? null) : null);
	const setup = $derived(plain.setups.tree);
	const missing = $derived(
		setup && info
			? missingColumns(
					setup,
					info.columns.map((c) => c.name),
				)
			: [],
	);
	const args = $derived(
		facts && table && info ? { dataset: facts.id, table, version: info.version } : null,
	);
	const editing = $derived(plain.editing.tree || setup === undefined);
	const profile = $derived(args && editing ? tableProfile(args) : null);
	const read = $derived(
		args && setup && !editing && missing.length === 0 ? derivedTree({ ...args, setup }) : null,
	);
	const tree = $derived(read?.current ?? null);
	const key = $derived(
		args && setup ? `${args.dataset}\u0000${args.table}\u0000${JSON.stringify(setup)}` : '',
	);
	const chosen = $derived(plain.node?.of === 'tree' ? plain.node : null);

	/** The drill and layout of this set-up's treemap; another set-up's start at the root. */
	const view = $derived(kept.key === key ? kept.view : 'drill');
	const drill = $derived(kept.key === key ? kept.drill : []);
	function keep(change: Partial<{ view: typeof kept.view; drill: string[] }>): void {
		if (kept.key !== key) {
			kept.view = 'drill';
			kept.drill = [];
			kept.key = key;
		}
		Object.assign(kept, change);
	}

	function choose(name: string, id: string | undefined): void {
		if (id === undefined) return;
		plain.pickNode('tree', id, name);
		if (dockControl.maximized !== 'tree') dockControl.reveal('results');
	}

	let width = $state(0);
	/** Maximized, the panel lists the node's rows itself, as a page of its own would. */
	const maximized = $derived(dockControl.maximized === 'tree');
	/** The panel's own width: the rows stand beside it only where both fit. */
	let panelWidth = $state(0);
	const beside = $derived(maximized && panelWidth >= 768);
	const n = (v: number): string => v.toLocaleString();
</script>

<section
	class="flex h-full min-h-0 flex-col"
	aria-labelledby="plain-tree-heading"
	bind:clientWidth={panelWidth}
>
	<h2 id="plain-tree-heading" class="sr-only">Tree</h2>
	{#if !args}
		<NoTable />
	{:else if editing}
		<div class="min-h-0 flex-1 overflow-auto">
			{#if profile?.error && !profile.current}
				<div class="grid h-full place-items-center">
					<LoadFailure
						what="The table's columns could not be read."
						detail={failureText(profile.error)}
						onretry={() => void profile.refresh()}
					/>
				</div>
			{:else if !profile?.current}
				<p class="text-muted-foreground p-6 text-center text-sm" role="status">
					Reading a sample of the table to suggest columns…
				</p>
			{:else}
				<SetupForm
					panel="tree"
					dataset={args.dataset}
					profile={profile.current}
					current={setup}
					onconfirm={(next) => plain.setUp('tree', next as TreeSetup)}
					oncancel={setup ? () => plain.edit('tree', false) : undefined}
				/>
			{/if}
		</div>
	{:else if missing.length > 0}
		<div class="m-auto flex max-w-sm flex-col items-center gap-2 p-6 text-center" role="alert">
			<p class="text-sm font-medium">This tree cannot be drawn</p>
			<p class="text-muted-foreground text-xs">
				Its set-up names {missing.length === 1 ? 'a column' : 'columns'} the table no longer holds:
				<span class="text-foreground font-mono">{missing.join(', ')}</span>.
			</p>
			<Button size="sm" onclick={() => plain.edit('tree')}>Set up again</Button>
		</div>
	{:else if read?.error && !tree}
		<div class="grid h-full place-items-center">
			<LoadFailure
				what="The tree could not be drawn."
				detail={failureText(read.error)}
				onretry={() => void read.refresh()}
			/>
		</div>
	{:else if !tree}
		<p class="text-muted-foreground m-auto flex items-center gap-2 text-sm" role="status">
			<Loader2 class="size-4 animate-spin" /> Nesting the rows…
		</p>
	{:else}
		{@const d = tree.diagnostics}
		<div
			class="border-border bg-card/40 flex flex-wrap items-center gap-2 border-b px-3 py-1.5 text-xs"
		>
			<span class="text-muted-foreground min-w-0" role="status">
				{n(d.nodes)} nodes, {d.depth} levels{d.orphans > 0
					? ` · ${n(d.orphans)} orphans at the top`
					: ''}{d.cycles > 0 ? ` · ${n(d.cycles)} on a cycle, left out` : ''}{d.cut_nodes > 0
					? ` · ${n(d.cut_nodes)} folded into “more”`
					: ''}{extentNote(d.extent) ? ` · ${extentNote(d.extent)}` : ''}{d.dropped_sizes > 0
					? ` · ${n(d.dropped_sizes)} rows left out: their size is no finite number`
					: ''}{d.clipped > 0 ? ` · ${n(d.clipped)} long values cut` : ''}
			</span>
			{#if chosen}
				<span class="text-foreground flex items-center gap-1"
					>The rows list <span class="font-medium">{chosen.name}</span>
					<Button
						variant="ghost"
						size="icon-xs"
						aria-label="Clear {chosen.name}"
						onclick={() => plain.reset()}><X /></Button
					></span
				>
			{/if}
			<Button class="ml-auto" variant="ghost" size="xs" onclick={() => plain.edit('tree')}>
				<Settings2 /> Change set-up
			</Button>
		</div>
		<div class="flex min-h-0 flex-1">
			<div class="relative min-h-0 min-w-0 flex-1" bind:clientWidth={width}>
				{#if width > 0}
					<TopicTreemap
						hierarchy={tree.hierarchy}
						noiseLabel=""
						unit={setup?.size ?? 'rows'}
						onShowResults={(name, node) => choose(name, node.id)}
						bind:view={() => view, (v) => keep({ view: v })}
						showNoise={true}
						bind:drill={() => drill, (v) => keep({ drill: v })}
					/>
				{/if}
			</div>
			{#if beside}
				<!-- Maximized, the tree stands in for its page: the rows under the chosen node, beside it. -->
				<div class="border-border w-[28rem] max-w-[45%] shrink-0 border-l"><RowsPanel /></div>
			{/if}
		</div>
	{/if}
</section>
