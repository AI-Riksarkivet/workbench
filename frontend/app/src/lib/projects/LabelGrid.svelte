<script lang="ts">
	// THE BULK-LABELING GRID: many items labelled quickly where the label is one decision about the whole item (a class the
	// ontology lets be drawn as a tag), from the keyboard. It lists the items open to the reader's labeling (their claims
	// and the items nobody holds) or, for a reviewer, the work waiting for them, a page at a time as the annotator filters
	// and pages them, with each item's work (`include=work`: its revision and tags) and what the reader may fire on it
	// (`legal_events`, the one rule the doors refuse by).
	//
	// A label is written to each item on its own, through the canvas's own save door (`label-run.ts`), claimed first where
	// its `legal_events` offer a claim; a refusal is that item's and is named on it. The last labels are undone and redone
	// by their inverse, built on the revision each label left, so work someone changed since is refused, not overwritten.
	// A model is asked about each selected item through the canvas's own assist door, while the person waits ("Ask a
	// model": one prompt of words for the whole selection, or the item alone), and its answer is saved to the item as
	// proposals on the revision the grid read. A model's proposals on an item are kept or removed all at once (Alt+A,
	// Alt+R) by the annotator's own door, and an item holding one is not sent to be submitted or accepted: the grid names
	// it instead, in the annotator's words (ADR 0019).
	//
	// role="grid": the cells are laid out in rows and columns and the keys move in both (arrows, Home/End, Page Up/Down),
	// with a cell's own link to the canvas inside it, which the grid pattern allows and a listbox's options do not.
	import { goto, replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import { untrack } from 'svelte';
	import { SvelteMap, SvelteSet } from 'svelte/reactivity';
	import type { Attachment } from 'svelte/attachments';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import { Dialog } from '@riksarkivet/ui/dialog';
	import * as Popover from '@riksarkivet/ui/popover';
	import { Select } from '@riksarkivet/ui/select';
	import { AudioLines, ExternalLink, Redo2, Sparkles, Undo2 } from '@lucide/svelte';

	import { linkWith } from '#lib/project.ts';
	import { taskCanvasHref } from '#lib/canvas/task-stream.ts';
	import { classColours, SETTLES_PROPOSALS, unsettledProposals } from '#lib/canvas/classes.ts';
	import { keyIsNotTheCanvas } from '#lib/canvas/walk-keys.ts';
	import {
		annotationsUrl,
		heldAsAt,
		revertSave,
		saveWork,
		settleProposals,
	} from '#lib/api/annotations.api.ts';
	import {
		allMatching,
		BULK_CAP,
		corpusRows,
		fireTaskEvent,
		pageTasks,
		readItem,
	} from '#lib/api/projects.api.ts';
	import { datasetView } from '#lib/api/explore.api.ts';
	import {
		fetchModels,
		requestAssist,
		type AssistListing,
		type AssistModel,
	} from '#lib/api/assist.api.ts';
	import { holdsRows } from '#lib/api/annotations.api.ts';
	import { formatClock } from '#lib/media/time/timeline.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import { bulkActions, targetsFor } from './bulk-events.ts';
	import { indexRows, rowFor, rowKeysFor } from './corpus-rows.ts';
	import { itemName } from './item-name.ts';
	import MediaThumb from './MediaThumb.svelte';
	import { ANNOTATOR_HINT, EVENT_LABELS, taskStateVariant } from './presentation.ts';
	import { taskImageUrl } from './task-media.ts';
	import { reauth } from '#lib/reauth.svelte.ts';
	import { personName } from './people.ts';
	import SendBackDialog from './SendBackDialog.svelte';
	import {
		between,
		carryCount,
		changeFor,
		detailedIn,
		digitOf,
		discards,
		excerpt,
		fits,
		gridClasses,
		gridAsk,
		gridDoorParams,
		gridLabelsOf,
		itemMoment,
		labelable,
		moved,
		readGridQuery,
		sentBackNote,
		writeGridQuery,
		GRID_SIZES,
		type GridQuery,
		type Intent,
	} from './label-grid.ts';
	import {
		applyIntent,
		askEach,
		pooled,
		revertEach,
		settleEach,
		type Control,
		type Doors,
		type Outcome,
		type Written,
	} from './label-run.ts';
	import type { TaskDetail } from './types.ts';

	let {
		projectId,
		permissions,
		me,
		classes,
		onchanged,
	}: {
		projectId: string;
		permissions: readonly string[];
		me: string | null;
		/** The project's classes: which a grid labels, and which are labelled on the canvas. */
		classes: readonly {
			name: string;
			colour?: string | null;
			tools?: string[];
			required?: boolean;
			attributes?: { required?: boolean }[];
		}[];
		/** After a change to items, so the page reads the project's counts again. */
		onchanged: () => void;
	} = $props();

	// THE ADDRESS IS THE VIEW: the pass, the filters, the page and the item the focus is on (`label-grid.ts`).
	let asked = $state.raw<URLSearchParams | null>(null);
	const query = $derived(readGridQuery(asked ?? page.url.searchParams));
	const filter = $derived(gridDoorParams(query, me).toString());
	const read = $derived(
		pageTasks({
			projectId,
			filter,
			limit: query.size,
			offset: (query.page - 1) * query.size,
			work: true,
		}),
	);
	const answer = $derived(read.current);
	const listing = $derived(answer?.ok ? answer.data : null);
	const tasks = $derived(listing?.details ?? []);
	const matched = $derived(listing?.matched ?? 0);

	function show(next: GridQuery): void {
		const address = writeGridQuery(next, asked ?? page.url.searchParams);
		asked = address;
		// oxlint-disable-next-line svelte/no-navigation-without-resolve -- the same path, its query changed
		void goto(`?${address}`, { reset: false }).finally(() => {
			if (asked === address) asked = null;
		});
	}
	/** A new filter or page starts with nothing selected. */
	function showList(next: GridQuery): void {
		clearSelection();
		focusId = null;
		lastIndex = 0;
		show({ ...next, at: null });
	}

	export function reload(): void {
		void read.refresh();
	}

	// THE CLASSES: the grid's labels with their digits, and the ones the canvas labels.
	const split = $derived(
		gridClasses(
			classes,
			classColours(
				classes.map((c) => ({
					name: c.name,
					colour: c.colour ?? null,
					tools: c.tools ?? [],
					required: c.required ?? false,
				})),
			),
		),
	);
	const labels = $derived(split.labels);
	const gridNames = $derived(new Set(labels.map((c) => c.name)));
	const mayReview = $derived(permissions.includes('can_review'));
	const mayAnnotate = $derived(permissions.includes('can_annotate'));

	// NAMES AND WORDS: each item's row and its dataset's declaration, for its name, a text's excerpt and a recording's
	// length. Decoration: an unreachable viewer costs them, never the grid.
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
	function described(task: TaskDetail): {
		name: string;
		text: string | null;
		seconds: number | null;
	} {
		const row = rowFor(task, rowIndex);
		const view = task.source.where ? viewReads.get(task.source.where)?.current : undefined;
		const fallback = task.source.keys[0] ?? task.task_id;
		if (!row || !view) return { name: fallback, text: null, seconds: null };
		const body = task.media?.kind === 'text' ? view.body(row) : '';
		return {
			name: itemName(view, row),
			text: body ? excerpt(body) : null,
			seconds: task.media?.kind === 'audio' ? view.duration(row) : null,
		};
	}

	// FOCUS AND SELECTION. One cell holds the tab stop (roving focus); the selection is task ids, and with "select all
	// matching" may reach past this page, whose items `known` keeps.
	// The focus is an ITEM, so a page read again (after a label, a submit) keeps it on that item; its place is derived.
	let focusId = $state<string | null>(null);
	let lastIndex = 0;
	const focusAt = $derived.by(() => {
		const id = focusId ?? query.at;
		const index = id === null ? -1 : tasks.findIndex((t) => t.task_id === id);
		return index >= 0 ? index : Math.max(0, Math.min(lastIndex, tasks.length - 1));
	});
	let anchor = $state(0);
	const selection = new SvelteSet<string>();
	let known = $state.raw(new Map<string, TaskDetail>());
	let columns = $state(4);
	const PAGE_ROWS = 3;

	// The cells' elements, to move the focus to: read only by the key handler, so a plain map rather than a reactive one.
	const cells = new Map<number, HTMLElement>();
	const cell =
		(index: number): Attachment<HTMLElement> =>
		(node) => {
			cells.set(index, node);
			return () => {
				if (cells.get(index) === node) cells.delete(index);
			};
		};

	/** True while the grid moves the focus itself, whose anchor the key handler has already set. */
	let moving = false;
	/** True from a pointer's press on a cell to its click, which sets the anchor itself (a Shift-click extends from it). */
	let pointing = false;
	function focusCell(index: number): void {
		const task = tasks[index];
		if (!task) return;
		focusId = task.task_id;
		lastIndex = index;
		moving = true;
		cells.get(index)?.focus();
		moving = false;
		// The address keeps the focused item, so a reload comes back to it: the entry is replaced in place, at once, with no
		// navigation, so no write of it can land after the person has gone to another tab.
		const address = writeGridQuery({ ...query, at: task.task_id }, asked ?? page.url.searchParams);
		// oxlint-disable-next-line svelte/no-navigation-without-resolve -- the same path, its query changed
		replaceState(`?${address}`, page.state);
	}

	// The address names an item the grid lists. One the focus or the address named that is gone (released, submitted, or
	// another's now) gives way to the item at the focus's place: the first listed on arrival, the one now where the focus
	// was during a session, so a person working by keyboard goes on where they were. In place, with no navigation.
	const addressed = $derived(focusId ?? query.at);
	const goneAt = $derived(
		listing && addressed !== null && !tasks.some((t) => t.task_id === addressed) ? addressed : null,
	);
	$effect(() => {
		if (goneAt === null) return;
		const at = tasks[focusAt]?.task_id ?? null;
		// Written outside what the effect tracks: the history entry it replaces is no state of the grid's.
		untrack(() => {
			focusId = at;
			const address = writeGridQuery({ ...query, at }, asked ?? page.url.searchParams);
			// oxlint-disable-next-line svelte/no-navigation-without-resolve -- the same path, its query changed
			replaceState(`?${address}`, page.state);
		});
	});

	function clearSelection(): void {
		selection.clear();
		known = new Map();
	}
	function selectRange(from: number, to: number): void {
		selection.clear();
		for (const i of between(from, to)) {
			const id = tasks[i]?.task_id;
			if (id) selection.add(id);
		}
	}
	function toggle(index: number): void {
		const id = tasks[index]?.task_id;
		if (!id) return;
		if (selection.has(id)) selection.delete(id);
		else selection.add(id);
		anchor = index;
	}

	const selected = $derived(
		[...selection]
			.map((id) => tasks.find((t) => t.task_id === id) ?? known.get(id))
			.filter((t): t is TaskDetail => t !== undefined),
	);
	const pageAllSelected = $derived(
		tasks.length > 0 && tasks.every((t) => selection.has(t.task_id)),
	);
	let selectingAll = $state(false);
	async function selectAllMatching(): Promise<void> {
		selectingAll = true;
		const result = await allMatching(projectId, filter, { work: true });
		selectingAll = false;
		if (!result.ok) {
			notice = { tone: 'error', text: result.detail, refused: [], skipped: [], left: [] };
			return;
		}
		known = new Map(result.data.tasks.map((t) => [t.task_id, t]));
		selection.clear();
		for (const t of result.data.tasks) selection.add(t.task_id);
		// The offer goes once all are selected, and the focus with it: it comes back to the grid, so the next key (a
		// digit) labels what was just selected.
		cells.get(focusAt)?.focus();
		if (result.data.capped)
			notice = {
				tone: 'error',
				text: `${matched.toLocaleString()} items match: the first ${BULK_CAP.toLocaleString()} are selected. Narrow the filter to label the rest.`,
				refused: [],
				skipped: [],
				left: [],
			};
	}

	// WRITES, one at a time in the order asked: a key pressed while a label is being written waits its turn, and each
	// runs on the items as the listing read them after the one before. A run says how far it is ("n of N") and may be
	// stopped: the items in flight finish (an ask in flight is given up), and the rest are left untouched and named as
	// not attempted.
	// Each item's last outcome, with the moment the item was at once the action settled (`itemMoment`): shown on its
	// cell only while the item is still there, so a refusal never outlives the item moving on. Null until the re-read.
	const outcomes = new SvelteMap<
		string,
		{
			status: Outcome['status'];
			text: string;
			proposed?: string;
			left?: string;
			moment: string | null;
		}
	>();
	const outcomeOf = (task: TaskDetail) => {
		const outcome = outcomes.get(task.task_id);
		return outcome && (outcome.moment === null || outcome.moment === itemMoment(task))
			? outcome
			: undefined;
	};
	/** Stamp the outcomes the last action left with the moment each item is at now that the listing is read again. */
	function stampOutcomes(): void {
		for (const [id, outcome] of outcomes) {
			if (outcome.moment !== null) continue;
			const task = tasks.find((t) => t.task_id === id) ?? known.get(id);
			if (task) outcomes.set(id, { ...outcome, moment: itemMoment(task) });
		}
	}
	interface Group {
		reason: string;
		names: string[];
	}
	/** How a summary reads: all as asked, stopped by the person (nothing failed), or something refused or not tried. */
	type Tone = 'ok' | 'stopped' | 'error';
	let notice = $state<{
		tone: Tone;
		text: string;
		refused: Group[];
		skipped: Group[];
		left: Group[];
	} | null>(null);
	let pending = $state(0);
	/** The run in flight: ``asks`` when Stop gives up the calls in flight (an ask of a model) rather than waits for them. */
	let run = $state<{ what: string; done: number; total: number; asks: boolean } | null>(null);
	let stopAsked = $state(false);
	/** How many items had settled when the person pressed Stop: the summary says the run stopped there. */
	let stoppedAt = $state<number | null>(null);
	/** Aborted by Stop, and when the grid goes (the page is left): what a run gives up rather than waits for. */
	let runAbort: AbortController | null = null;
	/** Aborted when the grid goes: the run in flight stops starting items, and the work queued behind it is dropped, so
	 *  nothing the grid began outlives the page. */
	const gone = new AbortController();
	$effect(() => () => {
		gone.abort();
		runAbort?.abort();
	});
	let chain: Promise<void> = Promise.resolve();
	function enqueue(work: () => Promise<void>): void {
		pending += 1;
		chain = chain
			.then(() => (gone.signal.aborted ? undefined : work()))
			.catch((err: unknown) => {
				notice = {
					tone: 'error',
					text: `NOT CONFIRMED: ${String(err)}`,
					refused: [],
					skipped: [],
					left: [],
				};
			})
			.finally(() => {
				pending -= 1;
				run = null;
			});
	}
	/** The control of one run: its progress shown as it settles, and the person's stop read before each item starts. A
	 *  session a service refused stops it too: the items not yet started are left as they were, for after signing in. */
	function control(what: string, total: number, asks = false): Control {
		stopAsked = false;
		stoppedAt = null;
		run = { what, done: 0, total, asks };
		const abort = new AbortController();
		runAbort = abort;
		return {
			progress: (done) => {
				if (run) run.done = done;
			},
			stopped: () => stopAsked || abort.signal.aborted || reauth.refused.length > 0,
			signal: abort.signal,
		};
	}
	function stop(): void {
		stopAsked = true;
		stoppedAt = run?.done ?? null;
		runAbort?.abort();
	}

	interface Entry {
		what: string;
		written: Written[];
	}
	let undos = $state.raw<Entry[]>([]);
	let redos = $state.raw<Entry[]>([]);
	/** How many label actions an undo reaches back. */
	const UNDO_DEPTH = 50;

	const unitUrl = (task: TaskDetail): string =>
		annotationsUrl({
			project: projectId,
			task: task.task_id,
			dataset: task.source.where ?? '',
			key: task.source.keys[0] ?? '',
		});
	const doors: Doors = {
		claim: (task) => fireTaskEvent({ taskId: task.task_id, event: 'claim' }),
		release: (task) => fireTaskEvent({ taskId: task.task_id, event: 'release' }),
		save: (task, payload) => saveWork(unitUrl(task), payload),
		settle: (task, action, revision) => settleProposals(unitUrl(task), action, revision),
		revert: (task, revision) => revertSave(unitUrl(task), revision),
		reread: (task) => readItem(projectId, task.task_id),
		undone: (task, revision) => heldAsAt(unitUrl(task), revision - 1),
		assist: (task, ask, signal) =>
			requestAssist(
				{
					project: projectId,
					task: task.task_id,
					dataset: task.source.where ?? '',
					key: task.source.keys[0] ?? '',
				},
				ask,
				signal,
			),
		holds: (task, ids) => holdsRows(unitUrl(task), ids),
	};

	/** The items as the listing answers them now (a selection past this page, as `known` read them). */
	const current = (ids: readonly string[]): TaskDetail[] =>
		ids
			.map((id) => tasks.find((t) => t.task_id === id) ?? known.get(id))
			.filter((t): t is TaskDetail => t !== undefined);

	/** ``items`` grouped by their reason, each group naming its items, the largest first. */
	function grouped(items: readonly { reason: string; name: string }[]): Group[] {
		const by = Map.groupBy(items, (i) => i.reason);
		return [...by]
			.map(([reason, group]) => ({ reason, names: group.map((i) => i.name) }))
			.sort((a, b) => b.names.length - a.names.length);
	}

	/** How a summary counts the items done and those left as they were. */
	interface Words {
		done: (n: number) => string;
		unchanged: string;
	}
	const CHANGED: Words = { done: (n) => `${n} changed`, unchanged: 'already so' };
	const MOVED: Words = { done: (n) => `${n} item${n === 1 ? '' : 's'}`, unchanged: 'already so' };
	const ANSWERED: Words = { done: (n) => `${n} answered`, unchanged: 'found nothing' };

	/** Report ``results`` of ``what`` over ``targets`` on each cell and in one summary: how many changed (a label), moved
	 *  (a transition) or were answered (an ask), how many were left as they were, the refusals grouped by their reason,
	 *  and what was not attempted. */
	function report(
		what: string,
		targets: readonly TaskDetail[],
		results: readonly Outcome[],
		words: Words = CHANGED,
	): void {
		const refused: { reason: string; name: string }[] = [];
		const skipped: { reason: string; name: string }[] = [];
		const left: { reason: string; name: string }[] = [];
		let done = 0;
		let unchanged = 0;
		targets.forEach((task, i) => {
			const result = results[i];
			if (!result) return;
			const name = described(task).name;
			switch (result.status) {
				case 'done':
					done += 1;
					if (result.left) left.push({ reason: result.left.group ?? result.left.reason, name });
					outcomes.set(task.task_id, {
						status: 'done',
						text: what,
						proposed: result.proposed,
						left: result.left?.reason,
						moment: null,
					});
					return;
				case 'unchanged':
					unchanged += 1;
					outcomes.delete(task.task_id);
					return;
				case 'refused':
					refused.push({ reason: result.group ?? result.reason, name });
					outcomes.set(task.task_id, { status: 'refused', text: result.reason, moment: null });
					return;
				case 'not-attempted':
					skipped.push({ reason: result.reason, name });
					outcomes.set(task.task_id, {
						status: 'not-attempted',
						text: result.reason,
						moment: null,
					});
			}
		});
		const parts = [
			words.done(done),
			left.length > 0 ? `${left.length} in part` : null,
			unchanged > 0 ? `${unchanged} ${words.unchanged}` : null,
			refused.length > 0 ? `${refused.length} refused` : null,
			skipped.length > 0 ? `${skipped.length} not attempted` : null,
		].filter(Boolean);
		// A run the person stopped says where it stopped, and is no failure: what was not started is as it was. Nor are
		// the items they chose to keep as they are.
		const stopped = stoppedAt !== null && skipped.length > 0;
		const unchosen = skipped.some((s) => !stopped && !s.reason.startsWith('kept'));
		notice = {
			tone:
				refused.length > 0 || left.length > 0 || unchosen ? 'error' : stopped ? 'stopped' : 'ok',
			text: stopped
				? `${what}: stopped at ${stoppedAt} of ${targets.length}; ${parts.join(', ')}.`
				: `${what}: ${parts.join(', ')}.`,
			refused: grouped(refused),
			skipped: grouped(skipped),
			left: grouped(left),
		};
	}

	async function settle(): Promise<void> {
		await read.refresh().catch(() => {});
		stampOutcomes();
		onchanged();
	}

	/** After a run that saved to items: its report, its saves on the undo stack as one entry, the items past this page
	 *  read again (they carry their new work for the next action on them), and the page read again. */
	async function finish(
		what: string,
		targets: readonly TaskDetail[],
		results: readonly Outcome[],
		words: Words = CHANGED,
	): Promise<void> {
		// A grid that is gone has nobody to tell, and no undo to keep.
		if (gone.signal.aborted) return;
		report(what, targets, results, words);
		const written: Written[] = [];
		results.forEach((r, i) => {
			const task = targets[i];
			if (task && r.status === 'done' && r.revision !== null)
				written.push({ task, revision: r.revision });
		});
		if (written.length > 0) {
			undos = [...undos, { what, written }].slice(-UNDO_DEPTH);
			redos = [];
		}
		if (known.size > 0) {
			const fresh = await allMatching(projectId, filter, { work: true });
			if (fresh.ok) known = new Map(fresh.data.tasks.map((t) => [t.task_id, t]));
		}
		await settle();
	}

	// ASKED ONCE: a label that would discard what another label carries (its text, attributes or links, or a model's
	// proposal) is asked about before anything is written, naming how many items; keeping them is the default.
	let asking = $state<{ count: number; total: number; what: string } | null>(null);
	let answer$: ((choice: 'skip' | 'replace' | 'cancel') => void) | null = null;
	function ask(count: number, total: number, what: string): Promise<'skip' | 'replace' | 'cancel'> {
		asking = { count, total, what };
		return new Promise((resolve) => {
			answer$ = (choice) => {
				asking = null;
				answer$ = null;
				resolve(choice);
			};
		});
	}

	/** Label ``ids`` (the selection, else the focused item) as ``intent`` asks. */
	function label(ids: readonly string[], intent: Intent): void {
		const what =
			intent.kind === 'clear'
				? 'Cleared'
				: intent.kind === 'set'
					? `Labelled ${intent.label}`
					: `Toggled ${intent.label}`;
		// The question is asked before anything is written, so it names what is asked, not what was done.
		const asked =
			intent.kind === 'clear'
				? 'Clear the labels'
				: intent.kind === 'set'
					? `Label ${intent.label}`
					: `Add ${intent.label}`;
		enqueue(async () => {
			const targets = current(ids);
			if (targets.length === 0) return;
			const risky = targets.filter(
				(t) =>
					labelable(t).kind !== 'no' &&
					discards(changeFor(t.work?.tags ?? [], intent, gridNames, () => '')),
			);
			let keep = new Set<string>();
			if (risky.length > 0) {
				const choice = await ask(risky.length, targets.length, asked);
				if (choice === 'cancel') return;
				if (choice === 'skip') keep = new Set(risky.map((t) => t.task_id));
			}
			const results = await applyIntent(
				targets,
				intent,
				gridNames,
				doors,
				control(what, targets.length),
				keep,
			);
			await finish(what, targets, results);
		});
	}

	/** Keep or remove every proposal of ``ids`` (the selection, else the focused item), each item on its own, undone by
	 *  the grid's undo as a label is. */
	function settleAll(ids: readonly string[], action: 'keep' | 'remove'): void {
		const what = action === 'keep' ? 'Kept the proposals' : 'Removed the proposals';
		enqueue(async () => {
			const targets = current(ids);
			if (targets.length === 0) return;
			const results = await settleEach(targets, action, doors, control(what, targets.length));
			await finish(what, targets, results);
		});
	}
	/** Ask ``model`` about each of ``ids`` (the selection, else the focused item), with ``words`` where it takes them, and
	 *  save each answer to its item as proposals, undone by the grid's undo as a label is. */
	function askAll(ids: readonly string[], model: AssistModel, words: string): void {
		const what = `Asked ${model.title || model.name}`;
		const ask = {
			model: model.name,
			prompt: gridAsk(model) === 'words' ? words.trim() : '',
			region: null,
			points: [],
		};
		enqueue(async () => {
			const targets = current(ids);
			if (targets.length === 0) return;
			const results = await askEach(
				targets,
				model,
				ask,
				doors,
				control(what, targets.length, true),
			);
			await finish(what, targets, results, ANSWERED);
		});
	}

	// THE MODELS the deployment's endpoint serves, read when the person opens them: those the grid can ask about many
	// items at once (`gridAsk`), with the one prompt of words they take, each judged against the items it would be asked
	// about (`fits`), since the items of one project may have been sent with different classes.
	let askOpen = $state(false);
	let models = $state.raw<AssistListing | null>(null);
	let modelsFailure = $state<string | null>(null);
	let askWords = $state('');
	const askable = $derived((models?.models ?? []).filter((m) => gridAsk(m) !== null));
	const anyWords = $derived(askable.some((m) => gridAsk(m) === 'words'));
	async function readModels(): Promise<void> {
		if (models !== null) return;
		modelsFailure = null;
		const answered = await fetchModels(projectId);
		if (answered.ok) models = answered.data;
		else modelsFailure = answered.detail;
	}
	function askWith(model: AssistModel): void {
		askOpen = false;
		askAll(targetIds(), model, askWords);
	}
	const askCount = $derived(selection.size > 0 ? selection.size : tasks[focusAt] ? 1 : 0);
	/** The items an ask would run on, as the grid has read them: the selection, else the item in focus. */
	const askTargets = $derived(current(targetIds()));
	/** How many of the items an ask would run on take a shape ``model`` returns. */
	const fitting = (model: AssistModel): number => askTargets.filter((t) => fits(model, t)).length;
	/** Whether ``model`` may be asked now: an item to ask, one that takes what it returns, and its words where it takes
	 *  them. */
	const askReady = (model: AssistModel): boolean =>
		askCount > 0 && fitting(model) > 0 && (gridAsk(model) !== 'words' || askWords.trim() !== '');
	/** The model Enter in the words asks: the first listed that takes words and may be asked now. */
	const enterModel = $derived(askable.find((m) => gridAsk(m) === 'words' && askReady(m)) ?? null);

	/** How many proposals ``items`` hold. */
	const proposalsIn = (items: readonly TaskDetail[]): number =>
		items.reduce((n, t) => n + (t.work?.proposals ?? 0), 0);

	/** Undo the last label (or redo the last undone), each item by the annotator's own undo of the save that wrote it. */
	function undo(redo = false): void {
		enqueue(async () => {
			const entry = (redo ? redos : undos).at(-1);
			if (!entry) return;
			const verb = redo ? 'Redone' : 'Undone';
			const results = await revertEach(
				entry.written,
				doors,
				control(`${verb}: ${entry.what}`, entry.written.length),
			);
			report(
				`${verb}: ${entry.what}`,
				entry.written.map((w) => w.task),
				results,
			);
			const back: Written[] = [];
			results.forEach((r, i) => {
				const w = entry.written[i];
				if (w && r.status === 'done' && r.revision !== null)
					back.push({ task: w.task, revision: r.revision });
			});
			if (redo) {
				redos = redos.slice(0, -1);
				if (back.length > 0) undos = [...undos, { what: entry.what, written: back }];
			} else {
				undos = undos.slice(0, -1);
				if (back.length > 0) redos = [...redos, { what: entry.what, written: back }];
			}
			await settle();
		});
	}

	/** The items an action acts on: the selection, or the focused item when nothing is selected. */
	function targetIds(): string[] {
		if (selection.size > 0) return [...selection];
		const focused = tasks[focusAt];
		return focused ? [focused.task_id] : [];
	}

	// TRANSITIONS: submit (and claim or release) for an annotator, accept and send back for a reviewer, each offered from
	// the selected items' own `legal_events`, fired per item and answered per item.
	const GRID_EVENTS = ['claim', 'release', 'submit', 'accept', 'fix_and_accept'];
	const available = $derived(
		bulkActions(selected, [...selection]).filter((a) => GRID_EVENTS.includes(a.event)),
	);
	const returnable = $derived(
		selected.filter((t) => t.legal_events.some((e) => e.event === 'request_changes')),
	);
	/** What a transition did, as its summary says it: the grid's own words for its buttons, in the past. */
	const DONE: Record<string, string> = {
		claim: 'Claimed',
		release: 'Released',
		submit: 'Submitted for review',
		accept: 'Accepted',
		fix_and_accept: 'Accepted with fixes',
		request_changes: 'Sent back',
	};
	function fireAll(event: string, message?: string): void {
		const ids = targetsFor(selected, [...selection], event).map((t) => t.task_id);
		const what = DONE[event] ?? (EVENT_LABELS[event] ?? event).replace('…', '');
		enqueue(async () => {
			const targets = current(ids);
			const results = await pooled(
				targets,
				async (task): Promise<Outcome> => {
					// A submission or an acceptance is a person's: an item holding a proposal nobody kept or removed is
					// named, not sent.
					const left = task.work?.proposals ?? 0;
					if (SETTLES_PROPOSALS.has(event) && left > 0)
						return { status: 'refused', reason: unsettledProposals(left, event) };
					const result = await fireTaskEvent({
						taskId: task.task_id,
						event,
						...(message ? { message } : {}),
					});
					return result.ok
						? { status: 'done', revision: 0 }
						: { status: 'refused', reason: result.detail };
				},
				control(what, targets.length),
				{ status: 'not-attempted', reason: 'stopped before it was started' },
			);
			report(what, targets, results, MOVED);
			// A transition leaves an item's work as it was, so an undo of a label written before it is still built on the
			// revision that label left.
			clearSelection();
			await settle();
		});
	}
	let sendBackOpen = $state(false);

	// KEYS, while the focus is in the grid and not on a field, a menu or a dialog (`walk-keys.ts`).
	function onkeydown(e: KeyboardEvent): void {
		if (keyIsNotTheCanvas(e)) return;
		const ctrl = e.ctrlKey || e.metaKey;
		const to = moved(focusAt, e.key, ctrl, {
			count: tasks.length,
			columns,
			pageRows: PAGE_ROWS,
		});
		if (to !== null) {
			e.preventDefault();
			if (e.shiftKey) selectRange(anchor, to);
			else anchor = to;
			focusCell(to);
			return;
		}
		const digit = digitOf(e.code);
		if (digit !== null && !ctrl) {
			const target = labels.find((c) => c.key === digit);
			if (!target) return;
			e.preventDefault();
			const single = selection.size === 0;
			label(targetIds(), { kind: e.altKey ? 'toggle' : 'set', label: target.name });
			// One item labelled from the focus moves on to the next, so a run of items is labelled key after key.
			if (single && focusAt < tasks.length - 1) {
				anchor = focusAt + 1;
				focusCell(focusAt + 1);
			}
			return;
		}
		if (ctrl && (e.key === 'z' || e.key === 'Z' || e.key === 'y')) {
			e.preventDefault();
			undo(e.key === 'y' || e.shiftKey);
			return;
		}
		if (ctrl && e.key === 'a') {
			e.preventDefault();
			selectRange(0, tasks.length - 1);
			return;
		}
		// Alt+A keeps and Alt+R removes every proposal of the selection (or the item in focus), as the canvas's do. Read from
		// the physical key, so they hold where Alt makes a letter of its own.
		if (e.altKey && !ctrl && !e.shiftKey && (e.code === 'KeyA' || e.code === 'KeyR')) {
			e.preventDefault();
			settleAll(targetIds(), e.code === 'KeyA' ? 'keep' : 'remove');
			return;
		}
		switch (e.key) {
			case ' ':
				e.preventDefault();
				toggle(focusAt);
				return;
			case 'Escape':
				if (selection.size === 0) return;
				e.preventDefault();
				clearSelection();
				return;
			case 'Delete':
			case 'Backspace':
				e.preventDefault();
				label(targetIds(), { kind: 'clear' });
				return;
			case 'Enter': {
				const task = tasks[focusAt];
				if (!task) return;
				e.preventDefault();
				// oxlint-disable-next-line svelte/no-navigation-without-resolve -- `link` resolves the path with the project
				void goto(link(taskCanvasHref(task, projectId)));
				return;
			}
		}
	}

	function onclick(e: MouseEvent, index: number): void {
		if (e.shiftKey) selectRange(anchor, index);
		else if (e.ctrlKey || e.metaKey) toggle(index);
		else {
			selection.clear();
			toggle(index);
		}
		focusCell(index);
		pointing = false;
	}

	/** A palette pick: the label on the selection (with Alt, beside the labels it holds), the focus back in the grid. */
	function pick(e: MouseEvent, name: string): void {
		label(targetIds(), { kind: e.altKey ? 'toggle' : 'set', label: name });
		cells.get(focusAt)?.focus();
	}

	// LAYOUT: as many columns as fit, measured, so Up and Down move by a row the person sees.
	const CELL_MIN = 176;
	const GAP = 12;
	const measure: Attachment<HTMLElement> = (node) => {
		const observer = new ResizeObserver(([entry]) => {
			const width = entry?.contentRect.width ?? node.clientWidth;
			columns = Math.max(1, Math.floor((width + GAP) / (CELL_MIN + GAP)));
		});
		observer.observe(node);
		return () => observer.disconnect();
	};
	const rows = $derived(
		Array.from({ length: Math.ceil(tasks.length / columns) }, (_, r) =>
			tasks
				.slice(r * columns, r * columns + columns)
				.map((task, c) => ({ task, index: r * columns + c })),
		),
	);

	// A picture is signed only once its cell nears the screen, so a page of 200 asks for what is looked at.
	const seen = new SvelteSet<string>();
	let observer: IntersectionObserver | null = null;
	const lazy =
		(taskId: string): Attachment<HTMLElement> =>
		(node) => {
			observer ??= new IntersectionObserver(
				(entries) => {
					for (const entry of entries) {
						const id = (entry.target as HTMLElement).dataset.task;
						if (entry.isIntersecting && id) seen.add(id);
					}
				},
				{ rootMargin: '400px 0px' },
			);
			node.dataset.task = taskId;
			observer.observe(node);
			return () => observer?.unobserve(node);
		};

	const link = $derived(linkWith(projectId));
	const tagsOf = (task: TaskDetail) => task.work?.tags ?? [];
	function cellName(task: TaskDetail): string {
		const held = gridLabelsOf(tagsOf(task), labels).map((c) => c.name);
		const proposed = gridLabelsOf(tagsOf(task), labels, true).map((c) => c.name);
		const outcome = outcomeOf(task);
		const note = sentBackNote(task);
		return [
			described(task).name,
			held.length > 0 ? `labelled ${held.join(', ')}` : 'no label',
			proposed.length > 0 ? `suggested ${proposed.join(', ')}` : null,
			(task.work?.proposals ?? 0) > 0 ? proposalCount(task.work?.proposals ?? 0) : null,
			detailedIn(tagsOf(task), gridNames) ? 'its label carries details' : null,
			outcome?.status === 'done' && outcome.proposed ? `proposed by ${outcome.proposed}` : null,
			outcome?.status === 'done' && outcome.left ? `left out: ${outcome.left}` : null,
			outcome?.status === 'refused' ? `refused: ${outcome.text}` : null,
			outcome?.status === 'not-attempted' ? `not attempted: ${outcome.text}` : null,
			note ? `sent back by ${personName(note.by, listing?.people)}: ${note.message}` : null,
		]
			.filter(Boolean)
			.join('; ');
	}
	const instructions = 'grid-keys';
	const proposalCount = (n: number): string =>
		`${n} proposal${n === 1 ? '' : 's'} to keep or remove`;
	/** The proposals the next keep or remove acts on: the selection's, or the focused item's. */
	const acting = $derived(
		proposalsIn(selection.size > 0 ? selected : tasks.slice(focusAt, focusAt + 1)),
	);
</script>

{#snippet keyHint(key: string)}
	<kbd class="bg-muted text-foreground border px-1 font-mono text-[11px]">{key}</kbd>
{/snippet}

<div class="flex min-w-0 flex-col gap-3" data-testid="label-grid-panel">
	<div class="flex flex-wrap items-center gap-2">
		<div class="flex items-center gap-1" role="group" aria-label="Whose items">
			<Button
				variant={query.pass === 'label' ? 'secondary' : 'ghost'}
				size="sm"
				aria-pressed={query.pass === 'label'}
				data-testid="grid-pass-label"
				onclick={() => showList({ ...query, pass: 'label', page: 1 })}
			>
				To label
			</Button>
			{#if mayReview}
				<Button
					variant={query.pass === 'review' ? 'secondary' : 'ghost'}
					size="sm"
					aria-pressed={query.pass === 'review'}
					data-testid="grid-pass-review"
					onclick={() => showList({ ...query, pass: 'review', page: 1 })}
				>
					To review
				</Button>
			{/if}
		</div>
		{#if (listing?.datasets.length ?? 0) > 1 || query.dataset}
			<Select
				value={query.dataset ?? ''}
				ariaLabel="Filter by dataset"
				onValueChange={(v) => showList({ ...query, dataset: v || null, page: 1 })}
				options={[
					{ value: '', label: 'Any dataset' },
					...(listing?.datasets ?? []).map((d) => ({ value: d, label: d })),
				]}
			/>
		{/if}
		{#if labels.length > 0}
			<Select
				value={query.label ?? ''}
				ariaLabel="Filter by label"
				onValueChange={(v) => showList({ ...query, label: v || null, page: 1 })}
				options={[
					{ value: '', label: 'Any label' },
					...labels.map((c) => ({ value: c.name, label: c.name })),
				]}
			/>
		{/if}
		{#if mayAnnotate && tasks.length > 0}
			<Popover.Root bind:open={askOpen} onOpenChange={(o) => o && void readModels()}>
				<Popover.Trigger>
					{#snippet child({ props })}
						<Button {...props} variant="outline" size="sm" data-testid="grid-assist-open">
							<Sparkles class="size-4" /> Ask a model
						</Button>
					{/snippet}
				</Popover.Trigger>
				<Popover.Content
					class="flex w-96 flex-col gap-3 p-3"
					align="start"
					data-testid="grid-assist-panel"
					onCloseAutoFocus={(e: Event) => {
						// Back to the item in focus once the panel is closed, so the grid's keys (Alt+A, Alt+R, Ctrl+Z) go on
						// where they were. The panel's focus scope also lets go while it stays open (it is set up again
						// when the panel's content changes), and moving the focus then would scroll the page from it.
						e.preventDefault();
						if (!askOpen) cells.get(focusAt)?.focus();
					}}
				>
					<h3 class="text-sm font-medium">
						Ask a model about {askCount} item{askCount === 1 ? '' : 's'}
					</h3>
					{#if modelsFailure}
						<p class="text-destructive text-xs" role="alert" data-testid="grid-assist-failed">
							The models could not be listed: {modelsFailure}
						</p>
					{:else if models === null}
						<p class="text-muted-foreground text-xs" role="status">Reading the models…</p>
					{:else if !models.available}
						<p class="text-muted-foreground text-xs" data-testid="grid-assist-unavailable">
							{models.reason.charAt(0).toUpperCase() + models.reason.slice(1)}.
						</p>
					{:else if askable.length === 0}
						<p class="text-muted-foreground text-xs" data-testid="grid-assist-none">
							The model endpoint serves no model the grid can ask: one that needs a drawn box or
							clicks is asked on the canvas, one item at a time.
						</p>
					{:else}
						{#if anyWords}
							<label class="flex flex-col gap-1 text-xs">
								<span class="text-muted-foreground">What to find, on every item</span>
								<input
									class="bg-background h-8 rounded-md border px-2 text-sm"
									bind:value={askWords}
									placeholder="e.g. stamp"
									data-testid="grid-assist-prompt"
									onkeydown={(e) => {
										if (e.key !== 'Enter' || e.isComposing) return;
										e.preventDefault();
										if (enterModel) askWith(enterModel);
									}}
								/>
								{#if enterModel}
									<span class="text-muted-foreground" data-testid="grid-assist-enter"
										>Enter finds it with {enterModel.title || enterModel.name}</span
									>
								{/if}
							</label>
						{/if}
						<ul class="flex flex-col gap-2">
							{#each askable as m (m.name)}
								<li
									class="flex flex-col gap-1 text-xs"
									data-testid="grid-assist-model"
									data-model={m.name}
								>
									<div class="flex items-center gap-2">
										<span class="min-w-0 flex-1 truncate font-medium" title={m.name}
											>{m.title || m.name}</span
										>
										{#if askTargets.length > 0 && fitting(m) < askTargets.length}
											<Badge
												variant="destructive"
												class="text-[10px]"
												title="What it returns ({m.returns.join(', ') ||
													'nothing named'}) is no shape the classes of {askTargets.length -
													fitting(m)} of these items take; those are not asked"
												data-testid="grid-assist-incompatible"
												>{fitting(m) === 0
													? 'does not fit'
													: `fits ${fitting(m)} of ${askTargets.length}`}</Badge
											>
										{/if}
										<Button
											size="xs"
											variant="outline"
											disabled={!askReady(m)}
											onclick={() => askWith(m)}>{gridAsk(m) === 'item' ? 'Ask' : 'Find'}</Button
										>
									</div>
									<span class="text-muted-foreground font-mono text-[11px]"
										>{m.name} · {m.version} · returns {m.returns.join(', ') ||
											'nothing named'}</span
									>
								</li>
							{/each}
						</ul>
						<p class="text-muted-foreground text-xs">
							Each item is asked on its own, claimed first where it is open, and its answer saved to
							it as proposals to keep or remove. An item whose classes take no shape a model returns
							is not asked.
						</p>
					{/if}
				</Popover.Content>
			</Popover.Root>
		{/if}
		<span class="text-muted-foreground ml-auto text-xs tabular-nums" data-testid="grid-count">
			{matched.toLocaleString()} item{matched === 1 ? '' : 's'}
		</span>
	</div>

	{#if labels.length > 0}
		<div
			class="flex flex-wrap items-center gap-1.5"
			role="toolbar"
			aria-label="Labels"
			data-testid="grid-palette"
		>
			{#each labels as c (c.name)}
				<Button
					variant="outline"
					size="sm"
					data-testid="grid-label-{c.name}"
					aria-keyshortcuts={c.key === null ? undefined : String(c.key)}
					title="Label the selection {c.name} (Alt adds it beside its other labels)"
					onclick={(e: MouseEvent) => pick(e, c.name)}
				>
					{#if c.key !== null}{@render keyHint(String(c.key))}{/if}
					<span class="size-2.5 shrink-0" style:background-color={c.colour} aria-hidden="true"
					></span>
					{c.name}
				</Button>
			{/each}
			<Button
				variant="ghost"
				size="sm"
				data-testid="grid-clear"
				aria-keyshortcuts="Delete"
				onclick={() => label(targetIds(), { kind: 'clear' })}
			>
				Clear {@render keyHint('Del')}
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				aria-label="Undo {undos.at(-1)?.what ?? ''}"
				aria-keyshortcuts="Control+Z"
				data-testid="grid-undo"
				disabled={undos.length === 0}
				onclick={() => undo()}
			>
				<Undo2 class="size-4" />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				aria-label="Redo {redos.at(-1)?.what ?? ''}"
				aria-keyshortcuts="Control+Shift+Z"
				data-testid="grid-redo"
				disabled={redos.length === 0}
				onclick={() => undo(true)}
			>
				<Redo2 class="size-4" />
			</Button>
		</div>
	{:else}
		<p class="text-muted-foreground text-sm" data-testid="grid-no-labels">
			This project has no class a grid labels: a grid label is a class drawn as a tag, one decision
			about the whole item.
		</p>
	{/if}
	{#if split.canvas.length > 0}
		<p class="text-muted-foreground text-xs" data-testid="grid-canvas-classes">
			Labelled on the canvas, one item at a time: {split.canvas
				.map((c) => `${c.name} (${c.why})`)
				.join(', ')}.
		</p>
	{/if}
	{#if proposalsIn(tasks) > 0 || proposalsIn(selected) > 0}
		<div
			class="flex flex-wrap items-center gap-1.5"
			role="toolbar"
			aria-label="Proposals"
			data-testid="grid-proposals"
		>
			<span class="text-muted-foreground text-xs" data-testid="grid-proposals-count"
				>{selection.size > 0 ? 'The selection' : 'The item in focus'} holds {acting} proposal{acting ===
				1
					? ''
					: 's'}</span
			>
			<Button
				variant="outline"
				size="sm"
				disabled={acting === 0}
				aria-keyshortcuts="Alt+A"
				data-testid="grid-keep-proposals"
				onclick={() => {
					settleAll(targetIds(), 'keep');
					cells.get(focusAt)?.focus();
				}}
			>
				Keep proposals {@render keyHint('Alt+A')}
			</Button>
			<Button
				variant="outline"
				size="sm"
				disabled={acting === 0}
				aria-keyshortcuts="Alt+R"
				data-testid="grid-remove-proposals"
				onclick={() => {
					settleAll(targetIds(), 'remove');
					cells.get(focusAt)?.focus();
				}}
			>
				Remove proposals {@render keyHint('Alt+R')}
			</Button>
		</div>
	{/if}
	{#if labels.length > 0}
		<!-- The grid's keys, said only where it has classes to label. -->
		<p id={instructions} class="text-muted-foreground text-xs" data-testid="grid-keys-help">
			Arrows move, Shift with an arrow selects, Space picks one, Ctrl+A the page; 1–9 label the
			selection (or the item in focus, then the next), Alt with a digit adds a label beside the
			others, Delete clears, Alt+A keeps and Alt+R removes every proposal, Ctrl+Z undoes, Enter
			opens the item on the canvas, Escape clears the selection.
		</p>
	{/if}

	{#if selection.size > 0}
		<div
			class="bg-muted/50 flex flex-wrap items-center gap-2 border px-3 py-1.5 text-sm"
			data-testid="grid-bulk-bar"
		>
			<span class="text-muted-foreground shrink-0">{selection.size} selected</span>
			<div class="ml-auto flex flex-wrap items-center justify-end gap-1">
				{#each available as action (action.event)}
					<Button
						variant={action.event === 'submit' || action.event === 'accept' ? 'default' : 'outline'}
						size="sm"
						data-testid="grid-bulk-{action.event}"
						onclick={() => fireAll(action.event)}
					>
						{(EVENT_LABELS[action.event] ?? action.event).replace('…', '')}
						{action.count}
					</Button>
				{/each}
				{#if returnable.length > 0}
					<Button
						variant="outline"
						size="sm"
						data-testid="grid-bulk-request_changes"
						onclick={() => (sendBackOpen = true)}
					>
						Send back {returnable.length}
					</Button>
				{/if}
				<Button variant="ghost" size="sm" onclick={clearSelection}>Clear the selection</Button>
			</div>
		</div>
	{/if}
	{#if pageAllSelected && matched > tasks.length && selection.size < Math.min(matched, BULK_CAP)}
		<p class="text-muted-foreground text-xs" data-testid="grid-select-all-offer">
			All {tasks.length} on this page are selected.
			<button
				type="button"
				class="text-foreground underline underline-offset-2"
				data-testid="grid-select-all-matching"
				disabled={selectingAll}
				onclick={() => void selectAllMatching()}
			>
				{selectingAll
					? 'Selecting…'
					: `Select all ${Math.min(matched, BULK_CAP).toLocaleString()} matching`}
			</button>
		</p>
	{/if}

	<div role="status" class="text-sm" data-testid="grid-notice">
		{#if run}
			<p
				class="text-muted-foreground flex flex-wrap items-center gap-2"
				data-testid="grid-progress"
			>
				<span class="tabular-nums">
					{run.what}: {run.done.toLocaleString()} of {run.total.toLocaleString()}{stopAsked
						? run.asks
							? ', giving up the asks in flight'
							: ', stopping after the items in flight'
						: ''}…
				</span>
				{#if run.total > 1}
					<Button
						variant="outline"
						size="xs"
						data-testid="grid-stop"
						disabled={stopAsked}
						onclick={stop}
					>
						Stop
					</Button>
				{/if}
			</p>
		{:else if pending > 0}
			<p class="text-muted-foreground">Writing…</p>
		{:else if notice}
			<p
				class={notice.tone === 'ok'
					? 'text-success'
					: notice.tone === 'stopped'
						? 'text-foreground'
						: 'text-destructive'}
				data-tone={notice.tone}
			>
				{notice.text}
			</p>
			{#if notice.refused.length > 0}
				<ul class="text-destructive mt-1 list-disc pl-5 text-xs" data-testid="grid-refusals">
					{#each notice.refused as g (g.reason)}
						<li>
							{g.reason} ({g.names.length}): {g.names.slice(0, 5).join(', ')}{g.names.length > 5
								? ` and ${g.names.length - 5} more, each marked in the grid`
								: ''}
						</li>
					{/each}
				</ul>
			{/if}
			{#if notice.left.length > 0}
				<ul class="text-destructive mt-1 list-disc pl-5 text-xs" data-testid="grid-left-out">
					{#each notice.left as g (g.reason)}
						<li>
							Left out of an answer, {g.reason} ({g.names.length}): {g.names
								.slice(0, 5)
								.join(', ')}{g.names.length > 5
								? ` and ${g.names.length - 5} more, each marked in the grid`
								: ''}
						</li>
					{/each}
				</ul>
			{/if}
			{#if notice.skipped.length > 0}
				<ul class="text-muted-foreground mt-1 list-disc pl-5 text-xs" data-testid="grid-skipped">
					{#each notice.skipped as g (g.reason)}
						<li>
							Not attempted, {g.reason} ({g.names.length}): {g.names.slice(0, 5).join(', ')}{g.names
								.length > 5
								? ` and ${g.names.length - 5} more`
								: ''}
						</li>
					{/each}
				</ul>
			{/if}
		{/if}
	</div>

	{#if answer && !answer.ok}
		<LoadFailure
			what="The project's items could not be read."
			detail={answer.detail}
			hint={ANNOTATOR_HINT}
			onretry={() => void read.refresh()}
		/>
	{:else if !listing}
		<p class="text-muted-foreground text-sm" role="status">Reading the items…</p>
	{:else if tasks.length === 0}
		<p class="text-muted-foreground border px-4 py-6 text-sm" data-testid="grid-empty">
			{query.pass === 'review'
				? 'Nothing waits for your review.'
				: 'Nothing is open to your labeling: every item is held by someone, or finished.'}
		</p>
	{:else}
		<div
			role="grid"
			tabindex="-1"
			aria-label="Items to label"
			aria-multiselectable="true"
			aria-rowcount={rows.length}
			aria-colcount={columns}
			aria-describedby={labels.length > 0 ? instructions : undefined}
			class="flex flex-col gap-3"
			data-testid="label-grid"
			{@attach measure}
			{onkeydown}
		>
			{#each rows as row, r (r)}
				<!-- `p-1` is the room a cell's focus ring is drawn in: the row's paint containment (`content-visibility`) clips
				     whatever it paints past its own box. -->
				<div
					role="row"
					aria-rowindex={r + 1}
					class="grid gap-3 p-1 [contain-intrinsic-block-size:auto_15.5rem] [content-visibility:auto]"
					style:grid-template-columns="repeat({columns}, minmax(0, 1fr))"
				>
					{#each row as { task, index }, c (task.task_id)}
						{@const about = described(task)}
						{@const held = gridLabelsOf(tagsOf(task), labels)}
						{@const outcome = outcomeOf(task)}
						{@const note = sentBackNote(task)}
						{@const picked = selection.has(task.task_id)}
						<!-- The grid takes every key for its cells (`onkeydown` on the grid): a click here is the mouse's way to the same. -->
						<!-- svelte-ignore a11y_click_events_have_key_events -->
						<div
							role="gridcell"
							aria-colindex={c + 1}
							aria-selected={picked}
							aria-label={cellName(task)}
							tabindex={index === focusAt ? 0 : -1}
							data-testid="grid-cell"
							data-task={task.task_id}
							data-selected={picked}
							class={[
								'group bg-card flex min-w-0 cursor-pointer flex-col overflow-hidden border outline-none select-none motion-safe:transition-colors',
								'focus-visible:ring-ring focus-visible:ring-3',
								picked ? 'border-primary ring-primary/50 ring-2' : 'hover:border-primary/60',
							]}
							{@attach cell(index)}
							{@attach lazy(task.task_id)}
							onpointerdown={() => (pointing = true)}
							onclick={(e) => onclick(e, index)}
							onfocus={() => {
								focusId = task.task_id;
								lastIndex = index;
								// Focus that arrives from outside (a Tab into the grid) anchors a range; the grid's own keys and a
								// click set the anchor themselves.
								if (!moving && !pointing) anchor = index;
							}}
						>
							{#if task.media?.kind === 'text'}
								<p
									class="bg-muted text-foreground h-28 overflow-hidden px-2 py-1.5 text-xs leading-snug"
									data-testid="grid-excerpt"
								>
									{about.text ?? 'Reading its text…'}
								</p>
							{:else if task.media?.kind === 'audio'}
								<div
									class="bg-muted text-muted-foreground flex h-28 items-center justify-center gap-2 text-xs"
								>
									<AudioLines class="size-6" aria-hidden="true" />
									{#if about.seconds !== null}
										<span class="tabular-nums" data-testid="grid-duration"
											>{formatClock(about.seconds)}</span
										>
									{/if}
								</div>
							{:else}
								<MediaThumb
									src={seen.has(task.task_id) ? taskImageUrl(task) : null}
									kind={task.media?.kind ?? 'image'}
									ratio="h-28"
								/>
							{/if}
							<div class="flex min-w-0 flex-col gap-1 p-2">
								<div class="flex min-w-0 items-center gap-1">
									<span class="truncate text-xs" title={`${about.name} · ${task.task_id}`}
										>{about.name}</span
									>
									<!-- `link` resolves the path through `linkWith`, with the reader's project after it. -->
									<!-- eslint-disable svelte/no-navigation-without-resolve -->
									<a
										class="text-muted-foreground hover:text-foreground ml-auto shrink-0"
										href={link(taskCanvasHref(task, projectId))}
										tabindex="-1"
										aria-hidden="true"
										title="Open on the canvas"
										onclick={(e) => e.stopPropagation()}
									>
										<ExternalLink class="size-3.5" />
									</a>
									<!-- eslint-enable svelte/no-navigation-without-resolve -->
								</div>
								<div
									class="flex min-h-5 flex-wrap items-center gap-1"
									data-testid="grid-cell-labels"
								>
									{#each held as c (c.name)}
										<span
											class="bg-secondary text-secondary-foreground inline-flex items-center gap-1 px-1.5 text-[11px]"
										>
											<span
												class="size-2 shrink-0"
												style:background-color={c.colour}
												aria-hidden="true"
											></span>
											{c.name}
										</span>
									{:else}
										<span class="text-muted-foreground text-[11px]">no label</span>
									{/each}
									{#each gridLabelsOf(tagsOf(task), labels, true) as c (c.name)}
										<span
											class="text-muted-foreground inline-flex items-center gap-1 border border-dashed px-1.5 text-[11px]"
											title="suggested by a model: kept by nobody yet; its label key keeps it"
											data-testid="grid-cell-suggested"
										>
											<span
												class="size-2 shrink-0"
												style:background-color={c.colour}
												aria-hidden="true"
											></span>
											{c.name}?
										</span>
									{/each}
								</div>
								<div class="flex flex-col items-start gap-1">
									<Badge variant={taskStateVariant(task.state)} class="text-[10px]">
										{task.state.replace('_', ' ')}
									</Badge>
									{#if detailedIn(tagsOf(task), gridNames)}
										<span
											class="text-muted-foreground text-[11px]"
											title="its label carries text, attributes, links or a model's proposal, which a new label would replace"
											data-testid="grid-cell-details"
										>
											has details
										</span>
									{/if}
									{#if (task.work?.proposals ?? 0) > 0}
										<span
											class="bg-warning/15 text-foreground px-1.5 text-[11px]"
											title="a model's proposals nobody kept or removed: the item is not submitted until each is"
											data-testid="grid-cell-proposals"
										>
											{proposalCount(task.work?.proposals ?? 0)}
										</span>
									{/if}
									{#if note}
										<p
											class="text-foreground border-warning line-clamp-2 min-w-0 border-l-2 pl-1.5 text-[11px] break-words group-focus:line-clamp-none"
											title={`${personName(note.by, listing?.people)}: ${note.message}`}
											data-testid="grid-cell-note"
										>
											<span class="font-medium">{personName(note.by, listing?.people)}:</span>
											{note.message}
										</p>
									{/if}
									{#if outcome?.status === 'done' && outcome.proposed}
										<span
											class="text-muted-foreground line-clamp-2 min-w-0 text-[11px] break-words group-focus:line-clamp-none"
											title="proposed by {outcome.proposed}"
											data-testid="grid-cell-assisted"
										>
											{outcome.proposed}
										</span>
									{/if}
									{#if outcome?.status === 'done' && outcome.left}
										<span
											class="text-destructive line-clamp-2 min-w-0 text-[11px] break-words group-focus:line-clamp-none"
											title="left out: {outcome.left}"
											data-testid="grid-cell-left-out"
										>
											left out: {outcome.left}
										</span>
									{/if}
									{#if outcome?.status === 'refused'}
										<span
											class="text-destructive line-clamp-2 min-w-0 text-[11px] break-words group-focus:line-clamp-none"
											title={outcome.text}
											data-testid="grid-cell-refused"
										>
											⚠ {outcome.text}
										</span>
									{:else if outcome?.status === 'not-attempted'}
										<span
											class="text-muted-foreground line-clamp-2 min-w-0 text-[11px] break-words group-focus:line-clamp-none"
											title={outcome.text}
											data-testid="grid-cell-skipped"
										>
											not attempted: {outcome.text}
										</span>
									{/if}
								</div>
							</div>
						</div>
					{/each}
				</div>
			{/each}
		</div>
		{#if matched > query.size}
			<nav class="flex items-center justify-end gap-2 text-xs" aria-label="Pages of items">
				<Select
					value={String(query.size)}
					ariaLabel="Items a page"
					onValueChange={(v) => showList({ ...query, size: Number(v), page: 1 })}
					options={GRID_SIZES.map((s) => ({ value: String(s), label: `${s} a page` }))}
				/>
				<Button
					variant="outline"
					size="sm"
					disabled={query.page <= 1}
					data-testid="grid-prev"
					onclick={() => showList({ ...query, page: query.page - 1 })}
				>
					Previous
				</Button>
				<span class="tabular-nums">
					{query.page} / {Math.max(1, Math.ceil(matched / query.size))}
				</span>
				<Button
					variant="outline"
					size="sm"
					disabled={query.page * query.size >= matched}
					data-testid="grid-next"
					onclick={() => showList({ ...query, page: query.page + 1 })}
				>
					Next
				</Button>
			</nav>
		{/if}
	{/if}
</div>

<Dialog.Root
	open={asking !== null}
	onOpenChange={(open) => {
		if (!open) answer$?.('cancel');
	}}
>
	<Dialog.Content
		class="sm:max-w-md"
		data-testid="grid-ask"
		onCloseAutoFocus={(e: Event) => {
			// Back to the item in focus, so the grid's keys go on where they were.
			e.preventDefault();
			cells.get(focusAt)?.focus();
		}}
	>
		<Dialog.Title>{asking?.what}: replace what other labels carry?</Dialog.Title>
		<Dialog.Description data-testid="grid-ask-count">
			{carryCount(asking?.count ?? 0, asking?.total ?? 0)} text, attributes, links or a model's proposal
			on a label that this one replaces. Skipping keeps those items as they are and labels the rest.
		</Dialog.Description>
		<form
			class="flex justify-end gap-2"
			onsubmit={(e) => {
				e.preventDefault();
				answer$?.('skip');
			}}
		>
			<Button type="submit" data-testid="grid-ask-skip">Skip those</Button>
			<Button
				type="button"
				variant="outline"
				data-testid="grid-ask-replace"
				onclick={() => answer$?.('replace')}
			>
				Replace them too
			</Button>
		</form>
	</Dialog.Content>
</Dialog.Root>

<SendBackDialog
	bind:open={sendBackOpen}
	title="Send back {returnable.length} item{returnable.length === 1 ? '' : 's'}"
	description="The note travels with each item to its annotator: say what should change on all of them."
	testid="grid-send-back"
	onsend={(note) => fireAll('request_changes', note)}
/>
