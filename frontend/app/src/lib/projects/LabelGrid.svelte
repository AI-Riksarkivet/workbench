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
	// SAMPLING (ADR 0034): while the project's strategy is on, the items are listed in its order unless the person asks
	// for item order, each with why it stands where it does; once a visit, the listed items with no number yet are read
	// and scored (`SamplingTopUp`), and more on the person's word.
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
	import { Select } from '@riksarkivet/ui/select';
	import { AudioLines, ExternalLink, Redo2, Undo2 } from '@lucide/svelte';

	import { linkWith } from '#lib/project.ts';
	import { taskCanvasHref } from '#lib/canvas/task-stream.ts';
	import { classColours, SETTLES_PROPOSALS, unsettledProposals } from '#lib/canvas/classes.ts';
	import { keyIsNotTheCanvas } from '#lib/canvas/walk-keys.ts';
	import {
		annotationsUrl,
		heldAsAt,
		proposalsOf,
		revertSave,
		revisionHolding,
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
	import { askJudgeAfterSubmit } from '#lib/api/judge.api.ts';
	import { itemsLacking, readSamples, requestSampling, READ_BATCH } from '#lib/api/sampling.api.ts';
	import { datasetView } from '#lib/api/explore.api.ts';
	import { requestAssist, type AssistModel, type AssistUnit } from '#lib/api/assist.api.ts';
	import { formatClock } from '#lib/media/time/timeline.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import { bulkActions, targetsFor } from './bulk-events.ts';
	import { indexRows, rowFor, rowKeysFor } from './corpus-rows.ts';
	import { itemName } from './item-name.ts';
	import GridAskPanel from './GridAskPanel.svelte';
	import MediaThumb from './MediaThumb.svelte';
	import { ANNOTATOR_HINT, EVENT_LABELS, taskStateVariant } from './presentation.ts';
	import { taskImageUrl } from './task-media.ts';
	import { reauth } from '#lib/reauth.svelte.ts';
	import { personName } from './people.ts';
	import JudgeQuestion from './JudgeQuestion.svelte';
	import SamplingReason from './SamplingReason.svelte';
	import { diverse, samplingLine, unaskedLine } from './sampling.ts';
	import { SamplingTopUp } from './sampling-top-up.svelte.ts';
	import SendBackDialog from './SendBackDialog.svelte';
	import {
		between,
		carryCount,
		changeFor,
		detailedIn,
		digitOf,
		discards,
		excerpt,
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
		redoEach,
		settleEach,
		undoEach,
		type Control,
		type Doors,
		type Outcome,
		type Run,
		type Written,
	} from './label-run.ts';
	import type { SamplingStrategy, TaskDetail } from './types.ts';

	let {
		projectId,
		permissions,
		me,
		classes,
		sampling = 'off',
		onchanged,
	}: {
		projectId: string;
		permissions: readonly string[];
		me: string | null;
		/** The project's sampling strategy: whether the grid lists in its order and shows each item's reason. */
		sampling?: SamplingStrategy;
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
	const samples = $derived(sampling !== 'off');
	const filter = $derived(gridDoorParams(query, me, samples).toString());
	const read = $derived(
		pageTasks({
			projectId,
			filter,
			limit: query.size,
			offset: (query.page - 1) * query.size,
			work: true,
			judge: true,
			sampling: samples,
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

	// SAMPLING'S LINE AND ITS TOP-UP: what the listing says of the project's sampling, and the round that reads and scores
	// the listed items with no number yet, once a visit for someone who may annotate whether or not the listing says the
	// strategy orders any of them yet (a round may be what makes it), and again on their word. It stops when the grid
	// goes.
	const samplingState = $derived(samples ? (listing?.sampling ?? null) : null);
	const samplingSaid = $derived(samplingState ? samplingLine(samplingState) : null);
	const topUp = new SamplingTopUp({
		strategy: () => sampling,
		lacking: (lane, offset, signal) => itemsLacking(projectId, filter, lane, offset, signal),
		reads: (ids, signal) => readSamples(projectId, ids, signal),
		ask: (task, signal) => requestSampling(task, signal),
		reread: async () => read.refresh(),
	});
	$effect(() => () => topUp.stop());
	$effect(() => {
		if (!samples || !mayAnnotate || listing === null) return;
		untrack(() => topUp.start());
	});

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
		const result = await allMatching(projectId, filter, { work: true, judge: true });
		selectingAll = false;
		if (!result.ok) {
			notice = { tone: 'error', text: result.detail, ...NO_GROUPS };
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
				...NO_GROUPS,
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
			stays?: string;
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
	/** A summary's items by what became of them: refused, not attempted, answered in part, and done with a claim that
	 *  stays the reader's. */
	interface Groups {
		refused: Group[];
		skipped: Group[];
		left: Group[];
		stays: Group[];
	}
	const NO_GROUPS: Groups = { refused: [], skipped: [], left: [], stays: [] };
	let notice = $state<({ tone: Tone; text: string } & Groups) | null>(null);
	let pending = $state(0);
	/** What Stop does to the items in flight: waits for them to finish, or gives up their calls (an ask of a model). */
	type Stop = 'waits' | 'gives-up';
	let run = $state<{ what: string; done: number; total: number; stop: Stop } | null>(null);
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
					...NO_GROUPS,
				};
			})
			.finally(() => {
				pending -= 1;
				run = null;
			});
	}
	/** The control of one run: its progress shown as it settles, and the person's stop read before each item starts. A
	 *  session a service refused stops it too: the items not yet started are left as they were, for after signing in. */
	function control(what: string, total: number, stop: Stop): Control {
		stopAsked = false;
		stoppedAt = null;
		run = { what, done: 0, total, stop };
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

	/** The unit an item's work is held on. Every run judges an item by `labelable` first, which refuses one that names no
	 *  unit, so an item reaching a door without one is a bug. */
	function unitOf(task: TaskDetail): AssistUnit {
		const dataset = task.source.where;
		const key = task.source.keys[0];
		if (!dataset || !key) throw new Error(`item ${task.task_id} names no unit of a dataset`);
		return { project: projectId, task: task.task_id, dataset, key };
	}
	const unitUrl = (task: TaskDetail): string => annotationsUrl(unitOf(task));
	const doors: Doors = {
		claim: (task) => fireTaskEvent({ taskId: task.task_id, event: 'claim' }),
		release: (task) => fireTaskEvent({ taskId: task.task_id, event: 'release' }),
		save: (task, payload) => saveWork(unitUrl(task), payload),
		settle: (task, action, revision) => settleProposals(unitUrl(task), action, revision),
		revert: (task, revision) => revertSave(unitUrl(task), revision),
		reread: (task) => readItem(projectId, task.task_id),
		undone: (task, revision) => heldAsAt(unitUrl(task), revision - 1),
		assist: (task, ask, signal) => requestAssist(unitOf(task), ask, signal),
		proposals: (task, source, signal) => proposalsOf(unitUrl(task), source, signal),
		revisionHolding: (task, ids) => revisionHolding(unitUrl(task), ids),
	};
	/** One run over ``total`` items, told as ``what``, whose Stop does as ``stop`` says. */
	const runOf = (what: string, total: number, stop: Stop = 'waits'): Run => ({
		doors,
		control: control(what, total, stop),
	});

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
		const stays: { reason: string; name: string }[] = [];
		let done = 0;
		let unchanged = 0;
		targets.forEach((task, i) => {
			const result = results[i];
			if (!result) return;
			const name = described(task).name;
			switch (result.status) {
				case 'done':
					if (result.left) left.push({ reason: result.left.group ?? result.left.reason, name });
					// Done with a claim that stays is not plainly done: it is counted and named on its own.
					if (result.stays) stays.push({ reason: result.stays.group ?? result.stays.reason, name });
					else done += 1;
					outcomes.set(task.task_id, {
						status: 'done',
						text: what,
						proposed: result.proposed,
						left: result.left?.reason,
						stays: result.stays?.reason,
						moment: null,
					});
					return;
				case 'unchanged': {
					// Left as it was with a claim that stays is not plainly so: it is counted and named on its own.
					if (result.stays) stays.push({ reason: result.stays.group ?? result.stays.reason, name });
					else unchanged += 1;
					const said = [result.note, result.stays?.reason].filter(Boolean).join('; ');
					if (said) outcomes.set(task.task_id, { status: 'unchanged', text: said, moment: null });
					else outcomes.delete(task.task_id);
					return;
				}
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
			stays.length > 0
				? `${stays.length} ${stays.length === 1 ? 'stays' : 'stay'} claimed by you`
				: null,
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
				refused.length > 0 || left.length > 0 || stays.length > 0 || unchosen
					? 'error'
					: stopped
						? 'stopped'
						: 'ok',
			text: stopped
				? `${what}: stopped at ${stoppedAt} of ${targets.length}; ${parts.join(', ')}.`
				: `${what}: ${parts.join(', ')}.`,
			refused: grouped(refused),
			skipped: grouped(skipped),
			left: grouped(left),
			stays: grouped(stays),
		};
	}

	/** What a run over ``targets`` wrote that an undo can take back: each item whose save it can name. */
	function writtenBy(targets: readonly TaskDetail[], results: readonly Outcome[]): Written[] {
		return results.flatMap((r, i) => {
			const task = targets[i];
			return task && r.status === 'done' && r.revision !== null
				? [{ task, revision: r.revision, claimed: r.claimed ?? null }]
				: [];
		});
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
		const written = writtenBy(targets, results);
		if (written.length > 0) {
			undos = [...undos, { what, written }].slice(-UNDO_DEPTH);
			redos = [];
		}
		if (known.size > 0) {
			const fresh = await allMatching(projectId, filter, { work: true, judge: true });
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
				{ intent, grid: gridNames, keep },
				runOf(what, targets.length),
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
			const results = await settleEach(targets, action, runOf(what, targets.length));
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
				{ model, ask },
				runOf(what, targets.length, 'gives-up'),
			);
			await finish(what, targets, results, ANSWERED);
		});
	}

	/** The items an ask would run on, as the grid has read them: the selection, else the item in focus. */
	const askTargets = $derived(current(targetIds()));

	/** How many proposals ``items`` hold. */
	const proposalsIn = (items: readonly TaskDetail[]): number =>
		items.reduce((n, t) => n + (t.work?.proposals ?? 0), 0);

	/** Replay ``entry`` by ``each`` (the annotator's own undo of each item's save, or of that undo), told as ``verb``:
	 *  what it wrote, for the opposite stack. */
	async function replay(
		entry: Entry,
		verb: 'Undone' | 'Redone',
		each: (written: readonly Written[], run: Run) => Promise<Outcome[]>,
	): Promise<Entry | null> {
		const what = `${verb}: ${entry.what}`;
		const results = await each(entry.written, runOf(what, entry.written.length));
		const tasks = entry.written.map((w) => w.task);
		report(what, tasks, results);
		const back = writtenBy(tasks, results);
		return back.length > 0 ? { what: entry.what, written: back } : null;
	}
	/** Undo the last label, each item by the annotator's own undo of the save that wrote it. */
	function undo(): void {
		enqueue(async () => {
			const entry = undos.at(-1);
			if (!entry) return;
			const back = await replay(entry, 'Undone', undoEach);
			undos = undos.slice(0, -1);
			if (back) redos = [...redos, back];
			await settle();
		});
	}
	/** Redo the last label undone, each item by the annotator's own undo of the undo. */
	function redo(): void {
		enqueue(async () => {
			const entry = redos.at(-1);
			if (!entry) return;
			const back = await replay(entry, 'Redone', redoEach);
			redos = redos.slice(0, -1);
			if (back) undos = [...undos, back];
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
	/** The items a send-back covers that a judge flagged, and whether the reviewer found those flags right. */
	const flaggedReturns = $derived(returnable.filter((t) => (t.judgement?.flags.length ?? 0) > 0));
	let flagsRight = $state(true);
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
					const wrong =
						event === 'request_changes' && !flagsRight && (task.judgement?.flags.length ?? 0) > 0;
					const result = await fireTaskEvent({
						taskId: task.task_id,
						event,
						...(message ? { message } : {}),
						...(wrong ? { judge_agreed: false } : {}),
					});
					if (result.ok && event === 'submit') askJudgeAfterSubmit(result.data);
					return result.ok
						? { status: 'done', revision: 0 }
						: { status: 'refused', reason: result.detail };
				},
				control(what, targets.length, 'waits'),
				{ status: 'not-attempted', reason: 'stopped before it was started' },
			);
			report(what, targets, results, MOVED);
			// A transition leaves an item's work as it was, so an undo of a label written before it is still built on the
			// revision that label left. The claim is the person's from now on, whatever a run took: an undo leaves it be.
			disown(targets.filter((_, i) => results[i]?.status === 'done').map((t) => t.task_id));
			clearSelection();
			await settle();
		});
	}
	/** The undo and redo stacks with no claim of a run left on ``ids``. */
	function disown(ids: readonly string[]): void {
		const moved = new Set(ids);
		const unclaimed = (entry: Entry): Entry => ({
			what: entry.what,
			written: entry.written.map((w) => (moved.has(w.task.task_id) ? { ...w, claimed: null } : w)),
		});
		undos = undos.map(unclaimed);
		redos = redos.map(unclaimed);
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
			if (e.key === 'y' || e.shiftKey) redo();
			else undo();
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
			outcome?.status === 'done' && outcome.stays ? outcome.stays : null,
			outcome?.status === 'unchanged' ? outcome.text : null,
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
		{#if samples}
			<div class="flex items-center gap-1" role="group" aria-label="Order">
				<Button
					variant={query.queue === 'sampling' ? 'secondary' : 'ghost'}
					size="sm"
					aria-pressed={query.queue === 'sampling'}
					data-testid="grid-order-sampling"
					onclick={() => showList({ ...query, queue: 'sampling', page: 1 })}
				>
					Sampling order
				</Button>
				<Button
					variant={query.queue === 'plain' ? 'secondary' : 'ghost'}
					size="sm"
					aria-pressed={query.queue === 'plain'}
					data-testid="grid-order-plain"
					onclick={() => showList({ ...query, queue: 'plain', page: 1 })}
				>
					Plain order
				</Button>
			</div>
		{/if}
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
			<GridAskPanel
				{projectId}
				targets={askTargets}
				onask={(model, words) => askAll(targetIds(), model, words)}
				onclose={() => cells.get(focusAt)?.focus()}
			/>
		{/if}
		<span class="text-muted-foreground ml-auto text-xs tabular-nums" data-testid="grid-count">
			{matched.toLocaleString()} item{matched === 1 ? '' : 's'}
		</span>
	</div>

	{#if samplingState && samplingSaid}
		{@const unasked = unaskedLine(samplingState)}
		<div class="flex flex-col gap-1 text-xs" data-testid="grid-sampling">
			<div class="flex flex-wrap items-center gap-2">
				<p
					class={samplingSaid.warn ? 'text-warning text-sm' : 'text-sm'}
					data-testid="grid-sampling-line"
				>
					{samplingSaid.text}{topUp.running ? ' Scoring…' : ''}
				</p>
				{#if mayAnnotate && !topUp.running && topUp.canContinue(samplingState)}
					<Button
						variant="outline"
						size="xs"
						data-testid="grid-sampling-more"
						onclick={() => void topUp.round()}
					>
						Score more
					</Button>
				{/if}
				{#if mayAnnotate && !topUp.running && diverse(samplingState.strategy) && tasks.length > 0}
					<Button
						variant="ghost"
						size="xs"
						data-testid="grid-sampling-measure"
						onclick={() => void topUp.measure(tasks.slice(0, READ_BATCH).map((t) => t.task_id))}
					>
						Measure again
					</Button>
				{/if}
			</div>
			{#if topUp.failure !== null}
				<p class="text-warning" role="alert" data-testid="grid-sampling-failure">
					The sampling model could not answer: {topUp.failure}
				</p>
			{/if}
			{#if topUp.refused !== null}
				<p class="text-destructive" role="alert" data-testid="grid-sampling-failure">
					Sampling could not read the items: {topUp.refused}
				</p>
			{/if}
			{#if unasked !== null && !topUp.running}
				<p class="text-muted-foreground" data-testid="grid-sampling-unasked">{unasked}</p>
			{/if}
			{#each topUp.notes as note (note)}
				<p class="text-muted-foreground" data-testid="grid-sampling-note">{note}</p>
			{/each}
		</div>
	{/if}

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
				onclick={undo}
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
				onclick={redo}
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
			others, Delete clears, Alt+A keeps and Alt+R removes every proposal, Ctrl+Z undoes and
			Ctrl+Shift+Z or Ctrl+Y redoes, Enter opens the item on the canvas, Escape clears the
			selection.
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
						onclick={() => {
							flagsRight = true;
							sendBackOpen = true;
						}}
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
			<Button
				variant="link"
				size="xs"
				data-testid="grid-select-all-matching"
				disabled={selectingAll}
				onclick={() => void selectAllMatching()}
			>
				{selectingAll
					? 'Selecting…'
					: `Select all ${Math.min(matched, BULK_CAP).toLocaleString()} matching`}
			</Button>
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
						? run.stop ===
							'gives-up'
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
			{#if notice.stays.length > 0}
				<ul class="text-destructive mt-1 list-disc pl-5 text-xs" data-testid="grid-stays">
					{#each notice.stays as g (g.reason)}
						<li>
							{g.reason} ({g.names.length}): {g.names.slice(0, 5).join(', ')}{g.names.length > 5
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
								{#if samples}
									<SamplingReason
										sampling={task.sampling}
										strategy={sampling}
										chosen={task.chosen}
									/>
								{/if}
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
									{#if outcome?.status === 'done' && outcome.stays}
										<span
											class="text-destructive line-clamp-2 min-w-0 text-[11px] break-words group-focus:line-clamp-none"
											title={outcome.stays}
											data-testid="grid-cell-stays"
										>
											{outcome.stays}
										</span>
									{/if}
									{#if outcome?.status === 'unchanged'}
										<span
											class="text-muted-foreground line-clamp-2 min-w-0 text-[11px] break-words group-focus:line-clamp-none"
											title={outcome.text}
											data-testid="grid-cell-unchanged"
										>
											{outcome.text}
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

{#snippet judgeQuestion()}
	<JudgeQuestion bind:agreed={flagsRight} count={flaggedReturns.length} />
{/snippet}

<SendBackDialog
	bind:open={sendBackOpen}
	title="Send back {returnable.length} item{returnable.length === 1 ? '' : 's'}"
	description="The note travels with each item to its annotator: say what should change on all of them."
	testid="grid-send-back"
	question={flaggedReturns.length > 0 ? judgeQuestion : undefined}
	onsend={(note) => fireAll('request_changes', note)}
/>
