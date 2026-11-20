<script lang="ts">
	// The DATA MANAGER: a project's items as one table a manager works in, and the queue an annotator and a reviewer
	// work from. The annotator filters, sorts and pages (`pageTasks`, `GET /projects/{id}/tasks`): the browser holds one
	// page, and every count beside it (how many match, how many each state holds) is the annotator's, from the same
	// snapshot. The address is the view: the filters, the sort, the page and its size are the page's URL
	// (`item-query.ts`), so a link, a reload and Back restore the screen.
	//
	// Every action button renders from the task's OWN `legal_events`: what the annotator says THIS reader may fire on it
	// now (their role, and the identity rules: no verdict on their own submission, no holder's event on another's
	// claim), the one rule the quick view and the canvas's panel read too. A denial is still the door's 403 or 409,
	// shown as it is; a disabled button is never the gate.
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import {
		createSvelteTable,
		DataTable,
		getCoreRowModel,
		renderSnippet,
		selectionColumn,
		sortableHeader,
		type ColumnDef,
		type PaginationState,
		type RowSelectionState,
		type SortingState,
	} from '@riksarkivet/ui/data-table';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import { Dialog } from '@riksarkivet/ui/dialog';
	import { Input } from '@riksarkivet/ui/input';
	import { ResizableSplit } from '@riksarkivet/ui/resizable-split';
	import { Select } from '@riksarkivet/ui/select';
	import { Textarea } from '@riksarkivet/ui/textarea';
	import { ExternalLink, Eye, LayoutGrid, Rows3, Trash2 } from '@lucide/svelte';

	import { linkWith } from '#lib/project.ts';
	import { taskCanvasHref } from '#lib/canvas/task-stream.ts';
	import { readOnlyReason } from '#lib/canvas/editability.ts';
	import {
		allMatching,
		BULK_CAP,
		corpusRows,
		dropTask,
		fetchMembers,
		fireTaskEvent,
		pageTasks,
	} from '#lib/api/projects.api.ts';
	import { datasetView } from '#lib/api/explore.api.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import { bulkActions, targetsFor } from './bulk-events.ts';
	import { indexRows, rowFor, rowKeysFor } from './corpus-rows.ts';
	import { corpusText, mediaText, predictedLabels } from './item-columns.ts';
	import ImportButton from './ImportButton.svelte';
	import TaskGrid from './TaskGrid.svelte';
	import { parseView, QUEUE_VIEW_KEY, type QueueView } from './queue-view.ts';
	import QuickView from './QuickView.svelte';
	import { afterVerdict } from './quick-view.ts';
	import LeaseChip from './LeaseChip.svelte';
	import PersonName from './PersonName.svelte';
	import { personName } from './people.ts';
	import { itemName } from './item-name.ts';
	import { ANNOTATOR_HINT, EVENT_LABELS, taskStateVariant, VERDICTS } from './presentation.ts';
	import {
		DATE_FIELDS,
		DEFAULT_QUERY,
		doorParams,
		filtering,
		readItemQuery,
		TASK_STATES,
		writeItemQuery,
		type ItemQuery,
		type Sort,
	} from './item-query.ts';
	import type { Adjudication, Contributor, People, TaskDetail, TaskState } from './types.ts';

	let {
		projectId,
		permissions,
		me,
		classes = [],
		contributors = [],
		contributorPeople = {},
		consensusN = 1,
		adjudications = {},
		droppable = false,
		onchanged,
	}: {
		projectId: string;
		/** The project permissions the reader holds: whether they manage (assign, the members), and whether an item opens
		 *  to annotate or to view. */
		permissions: readonly string[];
		me: string | null;
		/** The project's classes, for the filter by class. */
		classes?: readonly string[];
		/** Each person's part in the project (the annotator's count over every item), for the filters by person. */
		contributors?: readonly Contributor[];
		/** The names of those people. */
		contributorPeople?: People;
		/** The labeling task's `consensus_n`: labels the replica chip ("replica k/N"). */
		consensusN?: number;
		/** Group id → the manager's canonical pick: marks the winning replica. */
		adjudications?: Record<string, Adjudication>;
		/** Whether this project can still have items removed (draft/labeling). The door re-checks. */
		droppable?: boolean;
		/** Fired after any action that changed items, so the page reads the project again, and this tab (`reload`). */
		onchanged: () => void;
	} = $props();

	// The address a change asked for while its navigation is under way: the view is read from it until the URL holds it,
	// so a second change made before the first lands (a date field picked, then a day typed) builds on the first rather
	// than on the address the first is replacing.
	let asked = $state.raw<URLSearchParams | null>(null);
	const query = $derived(readItemQuery(asked ?? page.url.searchParams));
	const filter = $derived(doorParams(query, me).toString());
	const read = $derived(
		pageTasks({ projectId, filter, limit: query.size, offset: (query.page - 1) * query.size }),
	);
	const answer = $derived(read.current);
	const listing = $derived(answer?.ok ? answer.data : null);
	const people = $derived<People>(listing?.people ?? {});

	// SELECTION. Keyed by task id; `known` holds the selected items' documents, so a selection carried past this page
	// ("every item matching the filter") still knows each item's own legal events.
	let rowSelection = $state<RowSelectionState>({});
	let known = $state.raw(new Map<string, TaskDetail>());

	/** Go to the address that holds ``next``: a filter change goes back to the first page and clears the selection. */
	function show(next: ItemQuery, { keepPage = false }: { keepPage?: boolean } = {}): void {
		if (!keepPage) {
			next = { ...next, page: 1 };
			rowSelection = {};
			known = new Map();
		}
		const address = writeItemQuery(next, asked ?? page.url.searchParams);
		asked = address;
		// oxlint-disable-next-line svelte/no-navigation-without-resolve -- the same path, its query changed
		void goto(`?${address}`, { reset: false }).finally(() => {
			if (asked === address) asked = null;
		});
	}
	/** After an action changed items: the page reads the project again, this tab's reads with it (`reload`). */
	function refresh(): void {
		onchanged();
	}

	/** Read again everything this tab shows: the page of items with its counts, and the members a manager assigns from.
	 *  The project page calls it on its Refresh and when the reader comes back to the window. */
	export function reload(): void {
		void read.refresh();
		void membersRead?.refresh();
	}

	/** The item's labeling page, carrying the reader's project. */
	const link = $derived(linkWith(projectId));
	function canvasHref(task: TaskDetail): string {
		return link(taskCanvasHref(task, projectId));
	}
	function canvasVerb(task: TaskDetail): 'Annotate' | 'View' {
		return readOnlyReason(task, permissions, me) === null ? 'Annotate' : 'View';
	}

	const tasks = $derived(listing?.details ?? []);

	// The dataset row behind each item on this page, and its dataset's declaration, to call it by its document's title.
	// Decoration on a table that works without them: an unreachable viewer costs the names, never the table.
	const rowReads = $derived(
		[...Map.groupBy(tasks, (t) => t.source.where ?? null)].map(([dataset, group]) =>
			corpusRows({ dataset, keys: rowKeysFor(group) }),
		),
	);
	const rowIndex = $derived(
		new Map(
			rowReads.flatMap((r) => [...indexRows(r.current?.rows ?? [], r.current?.keyFields ?? [])]),
		),
	);
	const viewReads = $derived(
		new Map(
			[...new Set(tasks.map((t) => t.source.where).filter((w): w is string => !!w))].map((w) => [
				w,
				datasetView(w),
			]),
		),
	);
	function nameOf(task: TaskDetail): string | null {
		const row = rowFor(task, rowIndex);
		const view = task.source.where ? viewReads.get(task.source.where)?.current : undefined;
		return row && view ? itemName(view, row) : null;
	}
	const called = (task: TaskDetail): string => nameOf(task) ?? task.source.keys[0] ?? task.task_id;

	/** LIST or GRID, a per-browser preference: a list audits a queue, a grid scans its pictures. */
	let view = $state<QueueView>('list');
	onMount(() => {
		try {
			view = parseView(localStorage.getItem(QUEUE_VIEW_KEY));
		} catch {
			// A browser refusing storage keeps the default.
		}
	});
	function setView(next: QueueView): void {
		view = next;
		try {
			localStorage.setItem(QUEUE_VIEW_KEY, next);
		} catch {
			// Not kept; the view still changes.
		}
	}

	// QUICK VIEW: one item beside the table, walked with prev/next over this page.
	let quickId = $state<string | null>(null);
	let quickOpen = $state(false);
	function openQuick(task: TaskDetail): void {
		quickId = task.task_id;
		quickOpen = true;
	}

	let busy = $state<string | null>(null);
	let notice = $state<{ ok: boolean; text: string } | null>(null);
	let changesFor = $state<TaskDetail | null>(null);
	let changesMessage = $state('');

	async function fire(
		task: TaskDetail,
		event: string,
		opts?: { message?: string; assignee?: string },
	): Promise<void> {
		if (busy) return;
		busy = `${task.task_id}:${event}`;
		notice = null;
		const result = await fireTaskEvent({ taskId: task.task_id, event, ...opts });
		busy = null;
		if (result.ok) refresh();
		else notice = { ok: false, text: result.detail };
	}

	async function fireFromQuick(task: TaskDetail, event: string): Promise<void> {
		const next = afterVerdict(tasks, task.task_id);
		await fire(task, event);
		if (notice?.ok === false) return;
		quickId = next;
		quickOpen = next !== null;
	}

	const selectedIds = $derived(Object.keys(rowSelection).filter((id) => rowSelection[id]));
	const selected = $derived(
		selectedIds
			.map((id) => tasks.find((t) => t.task_id === id) ?? known.get(id))
			.filter((t): t is TaskDetail => t !== undefined),
	);
	const available = $derived(bulkActions(selected, selectedIds));
	const assignable = $derived(
		selected.filter((t) => t.legal_events.some((e) => e.event === 'assign')),
	);
	let bulkBusy = $state(false);
	let selectingAll = $state(false);
	const matched = $derived(listing?.matched ?? 0);
	const pageAllSelected = $derived(tasks.length > 0 && tasks.every((t) => rowSelection[t.task_id]));

	/** Select every item the filter matches (at most `BULK_CAP`), read from the annotator a page at a time. */
	async function selectAllMatching(): Promise<void> {
		selectingAll = true;
		const result = await allMatching(projectId, filter);
		selectingAll = false;
		if (!result.ok) {
			notice = { ok: false, text: result.detail };
			return;
		}
		const all = result.data.tasks;
		known = new Map(all.map((t) => [t.task_id, t]));
		rowSelection = Object.fromEntries(all.map((t) => [t.task_id, true]));
		if (result.data.capped)
			notice = {
				ok: false,
				text: `The filter matches ${matched.toLocaleString()} items: the first ${BULK_CAP.toLocaleString()} are selected. Narrow the filter to act on the rest.`,
			};
	}
	function clearSelection(): void {
		rowSelection = {};
		known = new Map();
	}

	/** Run ``act`` on each of ``targets``, ONE gated call per item, reported per item: there is no transaction across
	 *  items, so a refusal on one leaves the rest alone and is named. */
	async function eachOf(
		targets: TaskDetail[],
		act: (t: TaskDetail) => Promise<{ ok: true } | { ok: false; detail: string }>,
		done: string,
	): Promise<void> {
		if (bulkBusy || targets.length === 0) return;
		bulkBusy = true;
		notice = null;
		const failures: string[] = [];
		for (const task of targets) {
			const result = await act(task);
			if (!result.ok) failures.push(`${called(task)}: ${result.detail}`);
		}
		bulkBusy = false;
		clearSelection();
		notice =
			failures.length === 0
				? { ok: true, text: `${done}: ${targets.length} item${targets.length === 1 ? '' : 's'}.` }
				: {
						ok: false,
						text: `${targets.length - failures.length} of ${targets.length} — ${failures[0]}`,
					};
		refresh();
	}

	async function bulkFire(event: string): Promise<void> {
		const targets = targetsFor(selected, selectedIds, event);
		if (event === 'skip' && !confirm(`Skip ${targets.length} item(s)? They leave the queue.`))
			return;
		const label = (EVENT_LABELS[event] ?? event).replace('…', '');
		await eachOf(targets, (t) => fireTaskEvent({ taskId: t.task_id, event }), label);
	}

	async function bulkRemove(): Promise<void> {
		if (
			!confirm(
				`Remove ${selected.length} item(s) from the project? Their queued work is discarded.`,
			)
		)
			return;
		await eachOf([...selected], (t) => dropTask({ projectId, taskId: t.task_id }), 'Removed');
	}

	async function remove(task: TaskDetail): Promise<void> {
		if (busy !== null) return;
		if (!confirm('Remove this item from the project? Its queued work is discarded.')) return;
		busy = `${task.task_id}:drop`;
		notice = null;
		const result = await dropTask({ projectId, taskId: task.task_id });
		busy = null;
		notice = result.ok
			? { ok: true, text: result.data.removed ? 'Item removed.' : 'That item was already gone.' }
			: { ok: false, text: result.detail };
		if (result.ok) refresh();
	}

	// ASSIGN: a manager names who takes the items, chosen from the project's members by name (or typed as a principal).
	let assignFor = $state<TaskDetail[] | null>(null);
	let assignee = $state('');
	const membersRead = $derived(
		permissions.includes('can_manage') ? fetchMembers({ projectId }) : null,
	);
	const members = $derived(membersRead?.current?.ok ? membersRead.current.data : null);
	const memberPeople = $derived<People>({ ...contributorPeople, ...people, ...members?.people });
	const assignees = $derived(
		[
			...new Set(
				(members?.members ?? [])
					.filter((m) => m.relation !== 'viewer')
					.map((m) => m.user.replace(/^user:/, '')),
			),
		].sort(),
	);
	function assign(targets: TaskDetail[], to: string): void {
		void eachOf(
			targets,
			(t) => fireTaskEvent({ taskId: t.task_id, event: 'assign', assignee: to }),
			`Assigned to ${personName(to, memberPeople)}`,
		);
	}

	function rowEvents(task: TaskDetail): string[] {
		return task.legal_events
			.map((e) => e.event)
			.filter((e) => e !== 'assign' && e !== 'save_draft');
	}
	function replicaLabel(task: TaskDetail): string | null {
		if (!task.replica_of) return null;
		const k = /-r(\d+)$/.exec(task.task_id)?.[1];
		return k ? `replica ${k}/${consensusN}` : `replica of ${task.replica_of}`;
	}
	// What a row is: an item, or on a consensus project one of an item's replicas, each its own task and its own count.
	const rows = $derived(consensusN > 1 ? 'replicas' : 'items');
	const day = (iso: string | null | undefined): string =>
		iso ? new Date(iso).toLocaleDateString() : '';

	// The table, sorted and paged by the annotator: TanStack renders one page and reports what the person asked for.
	const SORTABLE: readonly Sort[] = ['item', 'state', 'assignee', 'submitted', 'reviewed', 'sent'];
	const sorting = $derived<SortingState>([{ id: query.sort, desc: query.order === 'desc' }]);
	const pagination = $derived<PaginationState>({
		pageIndex: query.page - 1,
		pageSize: query.size,
	});

	// A column that would say the same on every row is left out: the suggestions when no item on the page has one, the
	// dataset when the project's items all come from one, the media when every item on the page is of one kind.
	const columnVisibility = $derived({
		label: tasks.some((t) => t.prediction.length > 0),
		corpus: (listing?.datasets.length ?? 0) > 1,
		media: new Set(tasks.map((t) => t.media.kind)).size > 1,
	});

	const columns: ColumnDef<TaskDetail>[] = [
		selectionColumn<TaskDetail>(),
		{
			id: 'item',
			// The value TanStack needs to offer the sort; the annotator does the sorting.
			accessorFn: (t) => t.task_id,
			header: sortableHeader('item'),
			cell: ({ row }) => renderSnippet(itemCell, row.original),
			meta: { cellClass: 'whitespace-nowrap' },
		},
		{
			id: 'label',
			header: 'suggested',
			enableSorting: false,
			cell: ({ row }) => renderSnippet(labelCell, row.original),
		},
		{
			id: 'corpus',
			header: 'dataset',
			enableSorting: false,
			accessorFn: corpusText,
			meta: { cellClass: 'font-mono text-xs whitespace-nowrap' },
		},
		{
			id: 'media',
			header: 'media',
			enableSorting: false,
			accessorFn: mediaText,
			meta: { cellClass: 'text-xs' },
		},
		{
			id: 'state',
			// The value TanStack needs to offer the sort; the annotator does the sorting.
			accessorFn: (t) => t.state,
			header: sortableHeader('state'),
			cell: ({ row }) => renderSnippet(stateCell, row.original),
		},
		{
			id: 'assignee',
			// The value TanStack needs to offer the sort; the annotator does the sorting.
			accessorFn: (t) => t.assignee ?? '',
			header: sortableHeader('holder'),
			cell: ({ row }) => renderSnippet(leaseCell, row.original),
		},
		{
			id: 'submitted',
			// The value TanStack needs to offer the sort; the annotator does the sorting.
			accessorFn: (t) => t.submitted_at ?? '',
			header: sortableHeader('submitted'),
			cell: ({ row }) => renderSnippet(submittedCell, row.original),
		},
		{
			id: 'reviewed',
			// The value TanStack needs to offer the sort; the annotator does the sorting.
			accessorFn: (t) => t.reviewed_at ?? '',
			header: sortableHeader('reviewed'),
			cell: ({ row }) => renderSnippet(reviewedCell, row.original),
		},
		{
			id: 'sent',
			// The value TanStack needs to offer the sort; the annotator does the sorting.
			accessorFn: (t) => t.sent_at ?? '',
			header: sortableHeader('sent'),
			cell: ({ row }) => day(row.original.sent_at),
			meta: { cellClass: 'text-xs whitespace-nowrap' },
		},
		{
			id: 'actions',
			header: '',
			enableSorting: false,
			cell: ({ row }) => renderSnippet(actionsCell, row.original),
			meta: { cellClass: 'text-right' },
		},
	];

	const table = createSvelteTable({
		get data() {
			return tasks;
		},
		columns,
		getRowId: (task) => task.task_id,
		state: {
			get sorting() {
				return sorting;
			},
			get columnVisibility() {
				return columnVisibility;
			},
			get pagination() {
				return pagination;
			},
			get rowSelection() {
				return rowSelection;
			},
		},
		get pageCount() {
			return Math.max(1, Math.ceil(matched / query.size));
		},
		manualSorting: true,
		manualPagination: true,
		enableRowSelection: true,
		enableSortingRemoval: false,
		onSortingChange: (u) => {
			const next = typeof u === 'function' ? u(sorting) : u;
			const first = next[0];
			if (!first || !(SORTABLE as readonly string[]).includes(first.id)) return;
			show({ ...query, sort: first.id as Sort, order: first.desc ? 'desc' : 'asc' });
		},
		onPaginationChange: (u) => {
			const next = typeof u === 'function' ? u(pagination) : u;
			if (next.pageSize !== query.size) show({ ...query, size: next.pageSize });
			else show({ ...query, page: next.pageIndex + 1 }, { keepPage: true });
		},
		onRowSelectionChange: (u) => (rowSelection = typeof u === 'function' ? u(rowSelection) : u),
		getCoreRowModel: getCoreRowModel(),
	});

	/** The filter choices: each state with what choosing it would leave, the people who worked here, the classes. */
	const stateCounts = $derived(listing?.state_counts ?? {});
	const holders = $derived(contributors.filter((c) => c.open > 0 || c.submitted > 0));
	const submitters = $derived(contributors.filter((c) => c.submitted > 0));
	const reviewers = $derived(contributors.filter((c) => c.reviewed > 0));
	const byName = (c: Contributor): { value: string; label: string } => ({
		value: c.principal,
		label: personName(c.principal, contributorPeople),
	});
	function toggleState(state: TaskState): void {
		show({
			...query,
			states: query.states.includes(state)
				? query.states.filter((s) => s !== state)
				: [...query.states, state],
		});
	}
	const DATE_LABELS: Record<(typeof DATE_FIELDS)[number], string> = {
		updated: 'Last moved',
		sent: 'Sent',
		submitted: 'Submitted',
		reviewed: 'Reviewed',
	};
</script>

{#snippet itemCell(task: TaskDetail)}
	<div class="flex flex-col items-start gap-0.5">
		{#if nameOf(task)}
			<span class="text-sm" title={`${task.source.keys.join(', ')} · task ${task.task_id}`}
				>{nameOf(task)}</span
			>
		{:else}
			<span class="font-mono text-xs" title={task.task_id}
				>{task.source.keys.join(', ') || task.task_id}</span
			>
		{/if}
		{#if replicaLabel(task)}
			<span class="flex items-center gap-1">
				<Badge
					variant="outline"
					title="one of {consensusN} independent annotations of this item — group {task.replica_of}"
				>
					{replicaLabel(task)}
				</Badge>
				{#if task.replica_of && adjudications[task.replica_of]?.task_id === task.task_id}
					<Badge title="the manager's canonical pick for group {task.replica_of}">canonical</Badge>
				{/if}
			</span>
		{/if}
	</div>
{/snippet}

{#snippet stateCell(task: TaskDetail)}
	<Badge variant={taskStateVariant(task.state)}>{task.state.replace('_', ' ')}</Badge>
{/snippet}

{#snippet leaseCell(task: TaskDetail)}
	<LeaseChip {task} {me} {people} />
{/snippet}

{#snippet submittedCell(task: TaskDetail)}
	{#if task.submitted_by}
		<span class="text-xs whitespace-nowrap">
			<PersonName principal={task.submitted_by} {people} />
			<span class="text-muted-foreground">{day(task.submitted_at)}</span>
		</span>
	{/if}
{/snippet}

{#snippet reviewedCell(task: TaskDetail)}
	{#if task.reviewed_by}
		<span class="text-xs whitespace-nowrap">
			{VERDICTS[task.review_action ?? ''] ?? task.review_action} by
			<PersonName principal={task.reviewed_by} {people} />
			{#if task.review_notes.length > 0}
				<span
					class="text-muted-foreground"
					title={task.review_notes
						.map((n) => `${personName(n.by, people)}: ${n.message}`)
						.join('\n')}
				>
					· {task.review_notes.length} note{task.review_notes.length === 1 ? '' : 's'}
				</span>
			{/if}
		</span>
	{/if}
{/snippet}

{#snippet labelCell(task: TaskDetail)}
	<!-- A SUGGESTED label, not an accepted one: `secondary` keeps a machine's guess from looking like someone's work. -->
	<div class="flex flex-wrap gap-1">
		{#each predictedLabels(task) as label (label)}
			<Badge
				variant="secondary"
				title="suggested{task.prediction?.[0]?.source
					? ` (${task.prediction[0].source})`
					: ''} — not reviewed"
			>
				{label}
			</Badge>
		{/each}
	</div>
{/snippet}

{#snippet actionsCell(task: TaskDetail)}
	<div class="flex items-center justify-end gap-1 whitespace-nowrap">
		{#if droppable}
			<Button
				variant="ghost"
				size="icon-sm"
				disabled={busy !== null}
				data-testid="drop-task"
				aria-label="Remove {called(task)} from the project"
				title="Remove this item from the project (it cannot be completed)"
				onclick={() => void remove(task)}
			>
				<Trash2 class="size-3.5" />
			</Button>
		{/if}
		<Button
			variant="ghost"
			size="sm"
			data-testid="row-quick-view"
			aria-label="Review {called(task)} beside the table"
			title="Review this item beside the table"
			onclick={() => openQuick(task)}
		>
			<Eye class="size-3.5" />
		</Button>
		<Button
			variant="outline"
			size="sm"
			href={canvasHref(task)}
			aria-label="{canvasVerb(task)} {called(task)}"
			title="Open this item on the labeling canvas"
		>
			<ExternalLink class="size-3.5" />
			{canvasVerb(task)}
		</Button>
		{#if task.state === 'claimed'}
			<ImportButton taskId={task.task_id} onimported={refresh} />
		{/if}
		{#if task.legal_events.some((e) => e.event === 'assign')}
			<Button
				variant="outline"
				size="sm"
				disabled={busy !== null}
				title="assign this item to a named annotator (pinned — the lease never expires)"
				onclick={() => {
					assignFor = [task];
					assignee = '';
				}}
			>
				{EVENT_LABELS['assign']}
			</Button>
		{/if}
		{#each rowEvents(task) as event (event)}
			{#if event === 'request_changes'}
				<Button
					variant="outline"
					size="sm"
					disabled={busy !== null}
					onclick={() => {
						changesFor = task;
						changesMessage = '';
					}}
				>
					{EVENT_LABELS[event] ?? event}
				</Button>
			{:else}
				<Button
					variant={event === 'accept' ? 'default' : 'outline'}
					size="sm"
					disabled={busy !== null}
					title={(event === 'accept' || event === 'fix_and_accept') &&
					task.submitted_by &&
					me !== null &&
					task.submitted_by === me
						? 'you submitted this — the door refuses a self-review'
						: undefined}
					onclick={() => void fire(task, event)}
				>
					{EVENT_LABELS[event] ?? event}
				</Button>
			{/if}
		{/each}
	</div>
{/snippet}

{#snippet filters()}
	<div class="flex flex-col gap-2" data-testid="queue-filter">
		<!-- Each state with how many items it holds under the other filters: where the work sits, and the filter. -->
		<div class="flex flex-wrap items-center gap-1" role="group" aria-label="Filter by state">
			{#each TASK_STATES as state (state)}
				<Button
					variant={query.states.includes(state) ? 'secondary' : 'ghost'}
					size="xs"
					aria-pressed={query.states.includes(state)}
					data-testid="state-{state}"
					onclick={() => toggleState(state)}
				>
					{state.replace('_', ' ')}
					<span class="text-muted-foreground tabular-nums">{stateCounts[state] ?? 0}</span>
				</Button>
			{/each}
		</div>
		<div class="flex flex-wrap items-center gap-2">
			{#if classes.length > 0}
				<Select
					value={query.label ?? ''}
					ariaLabel="Filter by class"
					onValueChange={(v) => show({ ...query, label: v || null })}
					options={[
						{ value: '', label: 'Any class' },
						...classes.map((c) => ({ value: c, label: c })),
					]}
				/>
			{/if}
			<Select
				value={query.assignee ?? ''}
				ariaLabel="Filter by holder"
				onValueChange={(v) => show({ ...query, assignee: v || null })}
				options={[{ value: '', label: 'Held by anyone' }, ...holders.map(byName)]}
			/>
			<Select
				value={query.submitter ?? ''}
				ariaLabel="Filter by who submitted"
				onValueChange={(v) => show({ ...query, submitter: v || null })}
				options={[{ value: '', label: 'Submitted by anyone' }, ...submitters.map(byName)]}
			/>
			<Select
				value={query.reviewer ?? ''}
				ariaLabel="Filter by reviewer"
				onValueChange={(v) => show({ ...query, reviewer: v || null })}
				options={[{ value: '', label: 'Reviewed by anyone' }, ...reviewers.map(byName)]}
			/>
			{#if (listing?.datasets.length ?? 0) > 1 || query.dataset}
				<Select
					value={query.dataset ?? ''}
					ariaLabel="Filter by dataset"
					onValueChange={(v) => show({ ...query, dataset: v || null })}
					options={[
						{ value: '', label: 'Any dataset' },
						...(listing?.datasets ?? []).map((d) => ({ value: d, label: d })),
					]}
				/>
			{/if}
			<span class="flex flex-wrap items-center gap-1 text-xs">
				<Select
					value={query.date}
					ariaLabel="Which date the range reads"
					onValueChange={(v) => show({ ...query, date: v as ItemQuery['date'] })}
					options={DATE_FIELDS.map((d) => ({ value: d, label: DATE_LABELS[d] }))}
				/>
				<Input
					type="date"
					class="h-8 w-36"
					aria-label="From"
					data-testid="filter-since"
					value={query.since ?? ''}
					onchange={(e) => show({ ...query, since: e.currentTarget.value || null })}
				/>
				<span class="text-muted-foreground">to</span>
				<Input
					type="date"
					class="h-8 w-36"
					aria-label="To"
					data-testid="filter-until"
					value={query.until ?? ''}
					onchange={(e) => show({ ...query, until: e.currentTarget.value || null })}
				/>
			</span>
			<div class="ml-auto flex items-center gap-1" data-testid="queue-view-toggle">
				<Button
					variant={view === 'list' ? 'secondary' : 'ghost'}
					size="icon-sm"
					aria-label="List view"
					aria-pressed={view === 'list'}
					data-testid="view-list"
					onclick={() => setView('list')}
				>
					<Rows3 class="size-4" />
				</Button>
				<Button
					variant={view === 'grid' ? 'secondary' : 'ghost'}
					size="icon-sm"
					aria-label="Grid view"
					aria-pressed={view === 'grid'}
					data-testid="view-grid"
					onclick={() => setView('grid')}
				>
					<LayoutGrid class="size-4" />
				</Button>
			</div>
		</div>
		{#if filtering(query)}
			<p class="text-muted-foreground flex items-center gap-2 text-xs" data-testid="filter-count">
				{matched.toLocaleString()} of {(listing?.total ?? 0).toLocaleString()}
				{rows} match.
				<Button
					variant="ghost"
					size="xs"
					data-testid="clear-filter"
					onclick={() =>
						show({ ...DEFAULT_QUERY, sort: query.sort, order: query.order, size: query.size })}
				>
					Clear the filter
				</Button>
			</p>
		{/if}
	</div>
{/snippet}

{#snippet managerPane()}
	<div class="flex min-w-0 flex-col gap-2">
		{@render filters()}
		{#if selectedIds.length > 0}
			<div
				class="bg-muted/50 flex flex-wrap items-center gap-2 rounded-md border px-3 py-1.5 text-sm"
				data-testid="bulk-bar"
			>
				<span class="text-muted-foreground shrink-0">{selectedIds.length} selected</span>
				<div class="ml-auto flex flex-wrap items-center justify-end gap-1">
					{#each available as action (action.event)}
						<Button
							variant={action.event === 'accept' ? 'default' : 'outline'}
							size="sm"
							data-testid="bulk-{action.event}"
							disabled={bulkBusy}
							onclick={() => void bulkFire(action.event)}
						>
							{(EVENT_LABELS[action.event] ?? action.event).replace('…', '')}
							{action.count}
						</Button>
					{/each}
					{#if assignable.length > 0}
						<Button
							variant="outline"
							size="sm"
							data-testid="bulk-assign"
							disabled={bulkBusy}
							onclick={() => {
								assignFor = [...assignable];
								assignee = '';
							}}
						>
							Assign {assignable.length}
						</Button>
					{/if}
					{#if droppable}
						<Button
							variant="ghost"
							size="sm"
							data-testid="bulk-remove"
							disabled={bulkBusy}
							onclick={() => void bulkRemove()}
						>
							<Trash2 class="size-3.5" /> Remove {selected.length}
						</Button>
					{/if}
				</div>
			</div>
		{/if}
		<!-- SELECT THE REST: an offer, never automatic, so a bulk action never lands on rows nobody looked at unasked. -->
		{#if pageAllSelected && matched > tasks.length && selectedIds.length < Math.min(matched, BULK_CAP)}
			<p class="text-muted-foreground text-xs" data-testid="select-all-offer">
				All {tasks.length} on this page are selected.
				<button
					type="button"
					class="text-foreground underline underline-offset-2"
					data-testid="select-all-matching"
					disabled={selectingAll}
					onclick={() => void selectAllMatching()}
				>
					{selectingAll
						? 'Selecting…'
						: `Select all ${Math.min(matched, BULK_CAP).toLocaleString()} matching this filter`}
				</button>
			</p>
		{:else if selectedIds.length > tasks.length}
			<p class="text-muted-foreground text-xs" data-testid="select-all-held">
				{selectedIds.length.toLocaleString()} matching items are selected.
				<button
					type="button"
					class="text-foreground underline underline-offset-2"
					data-testid="select-all-clear"
					onclick={clearSelection}
				>
					Clear the selection
				</button>
			</p>
		{/if}
		{#if notice}
			<p
				class={notice.ok ? 'text-success text-sm' : 'text-destructive text-sm'}
				role="status"
				data-testid="queue-notice"
			>
				{notice.text}
			</p>
		{/if}

		{#if answer && !answer.ok}
			<LoadFailure
				what="The project's items could not be read."
				detail={answer.detail}
				hint={ANNOTATOR_HINT}
				onretry={() => void read.refresh()}
			/>
		{:else if !listing}
			<p class="text-muted-foreground text-sm" role="status">Reading the items…</p>
		{:else if listing.total === 0}
			<div
				class="flex flex-col gap-1 rounded-md border px-4 py-6 text-sm"
				data-testid="queue-empty"
			>
				<p class="font-medium">No items yet.</p>
				<p class="text-muted-foreground">
					Send items in from a dataset's search results or its atlas.
				</p>
			</div>
		{:else if view === 'grid' && tasks.length > 0}
			<TaskGrid {tasks} bind:selection={rowSelection} onopen={openQuick} />
		{:else}
			<DataTable
				{table}
				emptyMessage={filtering(query) ? 'No items match this filter.' : 'No items yet.'}
			/>
		{/if}
	</div>
{/snippet}

{#snippet quickPane()}
	<QuickView
		bind:open={quickOpen}
		bind:taskId={quickId}
		{tasks}
		{people}
		busy={busy !== null}
		onfire={(task, event) => void fireFromQuick(task, event)}
		{canvasHref}
		{canvasVerb}
		nameOf={called}
	/>
{/snippet}

{#if quickOpen}
	<ResizableSplit
		storageKey="annotator-queue-quickview"
		initial={0.62}
		minLeft={420}
		minRight={340}
	>
		{#snippet left()}
			{@render managerPane()}
		{/snippet}
		{#snippet right()}
			{@render quickPane()}
		{/snippet}
	</ResizableSplit>
{:else}
	{@render managerPane()}
{/if}

<Dialog.Root
	open={assignFor !== null}
	onOpenChange={(open) => (assignFor = open ? assignFor : null)}
>
	<Dialog.Content class="sm:max-w-sm" data-testid="assign-dialog">
		<Dialog.Title
			>Assign {assignFor?.length === 1
				? 'this item'
				: `${assignFor?.length ?? 0} items`}</Dialog.Title
		>
		<Dialog.Description>
			The person holds each with a pinned lease (it never expires) until they submit or release it.
			Each item is assigned on its own: a refusal on one leaves the rest alone.
		</Dialog.Description>
		<form
			class="flex flex-col gap-3"
			onsubmit={(e) => {
				e.preventDefault();
				const targets = assignFor;
				assignFor = null;
				if (targets && assignee.trim()) assign(targets, assignee.trim());
			}}
		>
			{#if assignees.length > 0}
				<Select
					bind:value={assignee}
					ariaLabel="Who takes the items"
					options={[
						{ value: '', label: 'Choose a member…' },
						...assignees.map((u) => ({ value: u, label: personName(u, memberPeople) })),
					]}
				/>
			{/if}
			<Input
				bind:value={assignee}
				aria-label="Their principal"
				placeholder="or their principal (idp~subject)"
			/>
			<div class="flex justify-end gap-2">
				<Button type="button" variant="outline" onclick={() => (assignFor = null)}>Cancel</Button>
				<Button type="submit" disabled={!assignee.trim()}>Assign</Button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>

<Dialog.Root
	open={changesFor !== null}
	onOpenChange={(open) => (changesFor = open ? changesFor : null)}
>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Title>Request changes</Dialog.Title>
		<Dialog.Description
			>The note travels with the item to its annotator: say what should change.</Dialog.Description
		>
		<form
			class="flex flex-col gap-3"
			onsubmit={(e) => {
				e.preventDefault();
				const task = changesFor;
				changesFor = null;
				if (task) void fire(task, 'request_changes', { message: changesMessage });
			}}
		>
			<Textarea
				bind:value={changesMessage}
				rows={4}
				aria-label="What should change"
				placeholder="The stamp in the corner is unlabelled…"
			/>
			<div class="flex justify-end gap-2">
				<Button type="button" variant="outline" onclick={() => (changesFor = null)}>Cancel</Button>
				<Button type="submit" disabled={!changesMessage.trim()}>Request changes</Button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>
