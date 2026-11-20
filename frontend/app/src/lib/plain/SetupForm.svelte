<script lang="ts">
	/**
	 * The set-up of one view of a table nobody stamped: which columns place the Atlas's points, link the
	 * Graph's nodes or nest the Tree's rows. The form opens with what the table's profile suggests
	 * (`./suggest.ts`): a role is filled in only where exactly one candidate is offered, and every candidate
	 * shows why it is offered. Nothing is drawn until the person confirms; nothing is written to the table.
	 *
	 * The Graph and the Tree show what the picked columns hold in the profile's sample of the table before
	 * they are drawn (distinct nodes, unmatched references, self-loops, empty rows, values that are no
	 * finite number, mixed kinds of key, orphans and cycles), asked of the viewer's check doors once the
	 * person has stopped changing the columns for a moment (`CHECK_AFTER_MS`), never of the whole view: a
	 * form being edited starts one small read, not a build per keystroke. It warns; it does not block.
	 */
	import { Button } from '@riksarkivet/ui/button';
	import { graphCheck, treeCheck, type TableProfile } from '#lib/api/derived.api.ts';
	import { failureText } from '#lib/failure.ts';
	import { prefillAtlas, prefillGraph, prefillTree, suggest, type Candidate } from './suggest.ts';
	import type { AtlasSetup, GraphSetup, SetupPanel, TreeSetup } from './setup.ts';

	type AnySetup = AtlasSetup | GraphSetup | TreeSetup;

	let {
		panel,
		dataset,
		profile,
		current,
		onconfirm,
		oncancel,
	}: {
		panel: SetupPanel;
		dataset: string;
		profile: TableProfile;
		/** The set-up in force, which the form edits; none opens it on the suggestions. */
		current: AnySetup | undefined;
		onconfirm: (setup: AnySetup) => void;
		/** Shown where a set-up is in force, to keep it. */
		oncancel?: (() => void) | undefined;
	} = $props();

	const id = $props.id();
	const suggestions = $derived(suggest(profile));

	/** The form's fields, every one a column name or '' for none; the shapes pick which fields apply. */
	interface Draft {
		place: 'columns' | 'vector';
		shape: string;
		[field: string]: string;
	}

	function draftOf(setup: AnySetup | undefined): Draft {
		const filled: Record<string, string | undefined> =
			setup !== undefined
				? { ...setup }
				: panel === 'atlas'
					? prefillAtlas(suggestions)
					: panel === 'graph'
						? prefillGraph(suggestions)
						: prefillTree(suggestions);
		const draft: Draft = {
			place: filled.place === 'vector' ? 'vector' : 'columns',
			shape: filled.shape ?? (panel === 'graph' ? 'edges' : 'parent'),
			separator: filled.separator ?? '/',
		};
		for (const [key, value] of Object.entries(filled)) {
			if (typeof value === 'string' && !['place', 'shape', 'separator'].includes(key))
				draft[key] = value;
		}
		return draft;
	}

	// The form edits its own copy; it opens on the set-up in force, or on the suggestions.
	// svelte-ignore state_referenced_locally
	let draft = $state<Draft>(draftOf(current));

	const NUMERIC = ['integer', 'float', 'decimal'];
	const KEYS = ['text', 'integer'];
	const LABELS = ['text', 'integer', 'float', 'decimal', 'boolean', 'date', 'timestamp'];

	interface Field {
		key: string;
		label: string;
		kinds: string[];
		required: boolean;
		candidates: Candidate<string>[];
		hint?: string;
	}

	const fields = $derived.by((): Field[] => {
		const s = suggestions;
		if (panel === 'atlas') {
			const place: Field[] =
				draft.place === 'vector'
					? [{ key: 'vector', label: 'Vector', kinds: ['vector'], required: true, candidates: [] }]
					: [
							{ key: 'x', label: 'Across (x)', kinds: NUMERIC, required: true, candidates: [] },
							{ key: 'y', label: 'Up (y)', kinds: NUMERIC, required: true, candidates: [] },
						];
			return [
				...place,
				{
					key: 'color',
					label: 'Colour by',
					kinds: ['text', 'boolean', ...NUMERIC],
					required: false,
					candidates: s.atlas.color,
				},
				{ key: 'label', label: 'Label', kinds: LABELS, required: false, candidates: s.atlas.label },
			];
		}
		if (panel === 'graph') {
			const self = draft.shape === 'self';
			return [
				{ key: 'source', label: self ? 'Id' : 'From', kinds: KEYS, required: true, candidates: [] },
				{
					key: 'target',
					label: self ? 'Points to' : 'To',
					kinds: KEYS,
					required: true,
					candidates: [],
				},
				{
					key: 'relation',
					label: 'Relation',
					kinds: ['text', 'integer', 'boolean'],
					required: false,
					candidates: s.graph.relation,
				},
				{
					key: 'weight',
					label: 'Weight',
					kinds: NUMERIC,
					required: false,
					candidates: s.graph.weight,
				},
				{
					key: 'label',
					label: self ? 'Node label' : 'Label of the “from” node',
					kinds: LABELS,
					required: false,
					candidates: s.graph.label,
				},
				{
					key: 'group',
					label: self ? 'Colour by' : 'Colour the “from” node by',
					kinds: ['text', 'integer', 'boolean'],
					required: false,
					candidates: s.graph.group,
				},
			];
		}
		if (draft.shape === 'path') {
			return [
				{ key: 'node', label: 'Path', kinds: ['text'], required: true, candidates: [] },
				{ key: 'size', label: 'Size', kinds: NUMERIC, required: false, candidates: s.tree.size },
			];
		}
		return [
			{ key: 'node', label: 'Id', kinds: KEYS, required: true, candidates: [] },
			{ key: 'parent', label: 'Parent', kinds: KEYS, required: true, candidates: [] },
			{ key: 'label', label: 'Label', kinds: LABELS, required: false, candidates: s.tree.label },
			{ key: 'size', label: 'Size', kinds: NUMERIC, required: false, candidates: s.tree.size },
		];
	});

	/** The main role's candidates: where the points sit, how the rows link, how they nest. */
	const leads = $derived.by((): { label: string; reason: string; apply: () => void }[] => {
		const s = suggestions;
		if (panel === 'atlas') {
			return s.atlas.place.map((c) => ({
				label: c.value.place === 'vector' ? c.value.vector : `${c.value.x}, ${c.value.y}`,
				reason: c.reason,
				apply: () => {
					draft = { ...draft, ...c.value } as Draft;
				},
			}));
		}
		if (panel === 'graph') {
			return s.graph.links.map((c) => ({
				label: `${c.value.source} → ${c.value.target}`,
				reason: c.reason,
				apply: () => {
					draft = { ...draft, ...c.value } as Draft;
				},
			}));
		}
		return s.tree.nesting.map((c) => ({
			label:
				c.value.shape === 'path'
					? `${c.value.node} (split at “${c.value.separator}”)`
					: `${c.value.node}, each under its ${c.value.parent}`,
			reason: c.reason,
			apply: () => {
				draft = { ...draft, ...c.value } as Draft;
			},
		}));
	});

	function options(field: Field): { suggested: Candidate<string>[]; other: string[] } {
		const suggested = field.candidates.filter((c) =>
			profile.columns.some((col) => col.name === c.value && field.kinds.includes(col.kind)),
		);
		const named = new Set(suggested.map((c) => c.value));
		const other = profile.columns
			.filter((col) => field.kinds.includes(col.kind) && !named.has(col.name))
			.map((col) => col.name);
		return { suggested, other };
	}

	/** The set-up the draft names, or why it cannot be confirmed yet. */
	const result = $derived.by((): { setup: AnySetup } | { missing: string } => {
		const empty = fields.find((f) => f.required && !draft[f.key]);
		if (empty) return { missing: `Pick the ${empty.label.toLowerCase()} column.` };
		const picked: Record<string, string> = {};
		for (const f of fields) if (draft[f.key]) picked[f.key] = draft[f.key]!;
		if (panel === 'atlas') {
			return { setup: { place: draft.place, ...picked } as AtlasSetup };
		}
		if (panel === 'graph') return { setup: { shape: draft.shape, ...picked } as GraphSetup };
		if (draft.shape === 'path') {
			if (!draft.separator) return { missing: 'Name the separator between a path’s levels.' };
			return { setup: { shape: 'path', separator: draft.separator, ...picked } as TreeSetup };
		}
		return { setup: { shape: 'parent', ...picked } as TreeSetup };
	});

	const args = $derived({ dataset, table: profile.table, version: profile.version });
	/** How long the columns stay unchanged before the check is asked. */
	const CHECK_AFTER_MS = 600;
	/** The draft's set-up as text, and the last one that stayed unchanged for `CHECK_AFTER_MS`. */
	const pending = $derived('setup' in result ? JSON.stringify(result.setup) : null);
	let settled = $state<string | null>(null);
	$effect(() => {
		const next = pending;
		const timer = setTimeout(() => (settled = next), CHECK_AFTER_MS);
		return () => clearTimeout(timer);
	});
	type Check =
		| { graph: ReturnType<typeof graphCheck>; tree?: undefined }
		| { tree: ReturnType<typeof treeCheck>; graph?: undefined }
		| null;
	/** What the settled draft's columns hold in the profile's sample. */
	const check: Check = $derived(
		!('setup' in result) || settled !== pending || panel === 'atlas'
			? null
			: panel === 'graph'
				? { graph: graphCheck({ ...args, setup: result.setup as GraphSetup }) }
				: { tree: treeCheck({ ...args, setup: result.setup as TreeSetup }) },
	);

	const n = (value: number): string => value.toLocaleString();

	/** What the read says, each a warning where it is one. */
	const findings = $derived.by((): { text: string; warn: boolean }[] => {
		if (check === null) return [];
		if (check.graph) {
			const d = check.graph.current;
			if (!d) return [];
			const self = draft.shape === 'self';
			return [
				{
					text: `Distinct nodes: ${n(d.nodes)}; distinct edges: ${n(d.edges)}`,
					warn: d.edges === 0,
				},
				{
					text: self
						? `“Points to” values that match no id: ${n(d.unmatched_targets)} (not drawn)`
						: `“To” values that never appear as a “from”: ${n(d.unmatched_targets)}`,
					warn: self && d.unmatched_targets > 0,
				},
				{ text: `Rows that point to themselves: ${n(d.self_loops)}`, warn: d.self_loops > 0 },
				{ text: `Rows with an empty end, left out: ${n(d.null_rows)}`, warn: d.null_rows > 0 },
				...(d.dropped_weights > 0
					? [
							{
								text: `Rows whose weight is no finite number, left out: ${n(d.dropped_weights)}`,
								warn: true,
							},
						]
					: []),
				...(d.mixed_types
					? [
							{
								text: 'The from and to columns hold different kinds of value: the integer 1 and the text “1” are two nodes',
								warn: true,
							},
						]
					: []),
				...(d.clipped_keys > 0
					? [{ text: `Ids longer than the viewer reads, cut: ${n(d.clipped_keys)}`, warn: true }]
					: []),
				...(d.cut_edges > 0 || d.cut_nodes > 0
					? [
							{
								text: `Too many to draw: the ${n(d.edges - d.cut_edges)} heaviest edges are kept; ${n(d.cut_edges)} edges and ${n(d.cut_nodes)} nodes are left out`,
								warn: true,
							},
						]
					: []),
			];
		}
		const d = check.tree?.current;
		if (!d) return [];
		return [
			{ text: `Nodes: ${n(d.nodes)}; levels: ${d.depth}`, warn: d.nodes === 0 },
			...(draft.shape === 'parent'
				? [
						{ text: `Roots: ${n(d.roots)}`, warn: d.roots === 0 },
						{
							text: `Rows whose parent is no row (shown at the top): ${n(d.orphans)}`,
							warn: d.orphans > 0,
						},
						{
							text: `Rows on a cycle no root reaches, left out: ${n(d.cycles)}`,
							warn: d.cycles > 0,
						},
					]
				: []),
			{
				text: `Rows with no ${draft.shape === 'path' ? 'path' : 'id'}, left out: ${n(d.null_rows)}`,
				warn: d.null_rows > 0,
			},
			...(d.dropped_sizes > 0
				? [
						{
							text: `Rows whose size is no finite number, left out: ${n(d.dropped_sizes)}`,
							warn: true,
						},
					]
				: []),
			...(d.clipped > 0
				? [{ text: `Values longer than the viewer reads, cut: ${n(d.clipped)}`, warn: true }]
				: []),
			...(d.cut_nodes > 0
				? [{ text: `Smaller nodes folded into “more”: ${n(d.cut_nodes)}`, warn: true }]
				: []),
		];
	});

	const extent = $derived(
		check === null
			? null
			: check.graph
				? (check.graph.current?.extent ?? null)
				: (check.tree?.current?.extent ?? null),
	);
	const checkRead = $derived(check === null ? null : (check.graph ?? check.tree ?? null));

	const TITLES: Record<SetupPanel, string> = {
		atlas: 'Set up the atlas',
		graph: 'Set up the graph',
		tree: 'Set up the tree',
	};
	const SHAPES: Record<SetupPanel, { value: string; label: string; hint: string }[]> = {
		atlas: [
			{ value: 'columns', label: 'Two numeric columns', hint: 'each row sits at its x and y' },
			{ value: 'vector', label: 'One vector column', hint: 'projected to two dimensions (PCA)' },
		],
		graph: [
			{ value: 'edges', label: 'Each row is an edge', hint: 'a from and a to column' },
			{ value: 'self', label: 'Each row points to another', hint: 'an id and a points-to column' },
		],
		tree: [
			{ value: 'parent', label: 'An id and a parent', hint: 'each row names its parent row' },
			{ value: 'path', label: 'One path column', hint: 'levels joined by a separator' },
		],
	};
	const shapeKey = $derived(panel === 'atlas' ? 'place' : 'shape');

	function confirm(event: SubmitEvent): void {
		event.preventDefault();
		if ('setup' in result) onconfirm(result.setup);
	}
</script>

<form
	class="mx-auto flex w-full max-w-xl flex-col gap-4 p-4 text-sm"
	aria-labelledby="{id}-title"
	onsubmit={confirm}
>
	<header class="flex flex-col gap-1">
		<h3 id="{id}-title" class="text-foreground text-base font-semibold">{TITLES[panel]}</h3>
		<p class="text-muted-foreground text-xs">
			Pick the columns of <span class="text-foreground font-mono">{profile.table}</span> that draw it.
			Nothing is written to the table; the choice is kept with this view.
		</p>
	</header>

	{#if leads.length > 0}
		<section aria-labelledby="{id}-suggested" class="flex flex-col gap-1.5">
			<h4
				id="{id}-suggested"
				class="text-muted-foreground text-xs font-medium tracking-wide uppercase"
			>
				Suggested from the schema
			</h4>
			<ul class="flex flex-col gap-1">
				{#each leads as lead (lead.label)}
					<li>
						<button
							type="button"
							class="border-border hover:bg-muted/60 focus-visible:ring-ring/50 flex w-full flex-col items-start rounded-md border px-2.5 py-1.5 text-left outline-none focus-visible:ring-3"
							onclick={lead.apply}
						>
							<span class="text-foreground font-mono text-xs">{lead.label}</span>
							<span class="text-muted-foreground text-xs">{lead.reason}</span>
						</button>
					</li>
				{/each}
			</ul>
			{#if leads.length > 1}
				<p class="text-muted-foreground text-xs">
					More than one fits, so none is filled in: choose one, or pick the columns below.
				</p>
			{/if}
		</section>
	{/if}

	<fieldset class="flex flex-col gap-1.5">
		<legend class="text-muted-foreground mb-1 text-xs font-medium tracking-wide uppercase"
			>{panel === 'atlas'
				? 'Place each point by'
				: panel === 'graph'
					? 'Link rows by'
					: 'Nest rows by'}</legend
		>
		{#each SHAPES[panel] as shape (shape.value)}
			<label class="flex items-start gap-2">
				<input
					type="radio"
					name="{id}-shape"
					value={shape.value}
					checked={draft[shapeKey] === shape.value}
					onchange={() => (draft = { ...draft, [shapeKey]: shape.value })}
					class="accent-primary mt-0.5"
				/>
				<span
					>{shape.label} <span class="text-muted-foreground text-xs">{` · ${shape.hint}`}</span></span
				>
			</label>
		{/each}
	</fieldset>

	<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
		{#each fields as field (field.key)}
			{@const choices = options(field)}
			{@const chosen = field.candidates.find((c) => c.value === draft[field.key])}
			<label class="flex min-w-0 flex-col gap-1">
				<span class="text-xs font-medium"
					>{field.label}{#if !field.required}<span class="text-muted-foreground ml-1 font-normal"
							>· optional</span
						>{/if}</span
				>
				<select
					class="border-input bg-background focus-visible:ring-ring/50 h-8 w-full rounded-md border px-2 text-sm outline-none focus-visible:ring-3"
					value={draft[field.key] ?? ''}
					onchange={(e) => (draft = { ...draft, [field.key]: e.currentTarget.value })}
				>
					<option value="">{field.required ? 'Choose a column…' : 'None'}</option>
					{#if choices.suggested.length > 0}
						<optgroup label="Suggested">
							{#each choices.suggested as c (c.value)}
								<option value={c.value}>{c.value} — {c.reason}</option>
							{/each}
						</optgroup>
					{/if}
					{#if choices.other.length > 0}
						<optgroup label={choices.suggested.length > 0 ? 'Other columns' : 'Columns'}>
							{#each choices.other as name (name)}
								<option value={name}>{name}</option>
							{/each}
						</optgroup>
					{/if}
				</select>
				{#if chosen}
					<span class="text-muted-foreground text-xs">Suggested: {chosen.reason}</span>
				{:else if choices.suggested.length + choices.other.length === 0}
					<span class="text-muted-foreground text-xs">No column of this table can be one.</span>
				{/if}
			</label>
		{/each}
		{#if panel === 'tree' && draft.shape === 'path'}
			<label class="flex flex-col gap-1">
				<span class="text-xs font-medium">Separator</span>
				<input
					type="text"
					maxlength="8"
					class="border-input bg-background focus-visible:ring-ring/50 h-8 rounded-md border px-2 font-mono text-sm outline-none focus-visible:ring-3"
					value={draft.separator}
					oninput={(e) => (draft = { ...draft, separator: e.currentTarget.value })}
				/>
			</label>
		{/if}
	</div>

	{#if checkRead}
		<section
			aria-labelledby="{id}-check"
			class="border-border bg-muted/30 flex flex-col gap-1 rounded-md border p-3 text-xs"
			role="status"
		>
			<h4 id="{id}-check" class="text-foreground font-medium">What these columns hold</h4>
			{#if checkRead.error && !checkRead.current}
				<p class="text-destructive">
					These columns could not be read: {failureText(checkRead.error)}
				</p>
			{:else if !checkRead.current}
				<p class="text-muted-foreground">Reading a sample of the table…</p>
			{:else}
				{#if extent}
					<p class="text-muted-foreground">
						{extent.sampled
							? `In a sample of ${n(extent.read)} of the table’s ${n(extent.total)} rows; the drawn view reads more.`
							: `In all ${n(extent.total)} rows.`}
					</p>
				{/if}
				<ul class="flex flex-col gap-0.5">
					{#each findings as finding (finding.text)}
						<li class={finding.warn ? 'text-amber-700 dark:text-amber-400' : 'text-foreground'}>
							{finding.warn ? '⚠ ' : ''}{finding.text}
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	{/if}

	<footer class="flex flex-wrap items-center justify-end gap-2">
		{#if 'missing' in result}
			<span class="text-muted-foreground mr-auto text-xs">{result.missing}</span>
		{/if}
		{#if oncancel}
			<Button type="button" variant="ghost" size="sm" onclick={oncancel}
				>Keep the current set-up</Button
			>
		{/if}
		<Button type="submit" size="sm" disabled={'missing' in result}>
			{panel === 'atlas'
				? 'Draw the atlas'
				: panel === 'graph'
					? 'Draw the graph'
					: 'Draw the tree'}
		</Button>
	</footer>
</form>
