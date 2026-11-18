<script lang="ts">
	/**
	 * One task's labeling page: the canvas over the task's unit, reached from the project's queue.
	 *
	 * The page names only the task; what it shows is read from the task's own record (its project, dataset, unit, media
	 * kind and captured ontology), so no link can point the canvas at an item the task does not hold. A URL naming
	 * another project than the task's goes to the task's own. The picture is a signed link the viewer makes for the
	 * person's own read; the annotations are the project's, read and written at the annotator's door with the project,
	 * the task and the dataset on every call (`#lib/api/annotations.api.ts`). The browser holds no store credential.
	 *
	 * ONE UNIT. A task's draft holds one unit's shapes, which the publish stamps with the task's first key, so the canvas
	 * opens that unit and says so when the item names more. A unit of a recording or a video opens its whole document
	 * on one time line, on the unit's own stretch: its regions and tracks are in seconds of the document.
	 *
	 * NAMES. The page reads the item's row and its dataset's declaration to call it by its document's title and where
	 * in it the item is, and names the breadcrumb's project and item so, the ids kept as their tooltips.
	 *
	 * WHO EDITS. The reader's permissions on the project and the task's state and holder decide whether the canvas takes
	 * edits (`readOnlyReason`); otherwise it opens read-only and says why. The doors decide on their own.
	 */
	import { untrack } from 'svelte';
	import { corpusRows, fetchProject, fetchTask, pageTasks } from '#lib/api/projects.api.ts';
	import {
		passParams,
		passWindow,
		readItemQuery,
		readPass,
		readPassCount,
		withPassCount,
		writeItemQuery,
	} from '#lib/projects/item-query.ts';
	import { datasetView } from '#lib/api/explore.api.ts';
	import { documentParts, documentTurns } from '#lib/api/timeline.api.ts';
	import { itemName } from '#lib/projects/item-name.ts';
	import { nameCrumb } from '#lib/shell/crumb-names.svelte.ts';
	import type { CanvasMedia, Part } from '#lib/canvas/media.ts';
	import type { TaskDetail } from '#lib/projects/types.ts';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { linkWith } from '#lib/project.ts';
	import { titleOf } from '#lib/nav.ts';
	import { annotationsUrl } from '#lib/api/annotations.api.ts';
	import { mediaLinks } from '#lib/api/media.api.ts';
	import type { MediaItem } from '#lib/api/media-links.svelte.ts';
	import { gpuUnsupportedReason } from '#lib/gpu/gpu-support.ts';
	import { rowKeyFor } from '#lib/projects/corpus-rows.ts';
	import { ANNOTATOR_HINT } from '#lib/projects/presentation.ts';
	import { signInAgainOnce, signedInAt } from '#lib/reauth.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import CanvasShell from '#lib/canvas/CanvasShell.svelte';
	import { readOnlyReason } from '#lib/canvas/editability.ts';
	import {
		claimedStream,
		neighbour,
		passHref,
		streamPosition,
		taskCanvasHref,
		type StreamTask,
	} from '#lib/canvas/task-stream.ts';

	let { data } = $props();

	const projectId = $derived(page.params.project ?? '');
	const taskId = $derived(page.params.task ?? '');
	const link = $derived(linkWith(projectId));
	const exitHref = $derived(link(`/projects/${encodeURIComponent(projectId)}`));
	const me = $derived(data.me?.sub ?? null);

	let gpuReason = $state<string | null | undefined>(undefined);
	void gpuUnsupportedReason().then((reason) => (gpuReason = reason));

	const taskRead = $derived(fetchTask({ taskId }));
	const answer = $derived(taskRead.current);
	/** A transition the side panel made answers the task as it is now, shown until the next read of it. */
	let changed = $state<TaskDetail | null>(null);
	const task = $derived(changed?.task_id === taskId ? changed : answer?.ok ? answer.data : null);
	function ontaskchange(next: TaskDetail): void {
		// A review pass moves on once the item leaves review: to the next item of the walk as it was before the verdict
		// (the item has left the filter, so the walk read again would not hold its neighbours), one more verdict counted
		// in the address, or, past the last, back to the project's Review tab.
		const after =
			pass === 'review' && next.state !== 'in_review'
				? (neighbour(walkItems, taskId, 1) ?? neighbour(walkItems, taskId, -1))
				: undefined;
		const moveTo =
			after === undefined
				? null
				: after === null
					? reviewDoneHref
					: link(hrefWith(passCount.done + 1)(after, projectId));
		changed = next;
		void taskRead.refresh().then(() => (changed = null));
		void walkRead.refresh();
		// oxlint-disable-next-line svelte/no-navigation-without-resolve -- the walk's hrefs are resolved (`linkWith`)
		if (moveTo !== null) void goto(moveTo);
	}
	const elsewhere = $derived(task !== null && task.project_id !== projectId);

	// A URL naming another project than the task's: the task's own page, so its queue and permissions are its project's.
	$effect(() => {
		if (!task || !elsewhere) return;
		const real = task.project_id;
		// oxlint-disable-next-line svelte/no-navigation-without-resolve -- `linkWith` resolves the path through `$app/paths`
		void goto(linkWith(real)(taskCanvasHref(task, real)), { replaceState: true });
	});

	const detailRead = $derived(task && !elsewhere ? fetchProject({ projectId }) : null);
	const permissions = $derived(
		detailRead?.current?.ok ? detailRead.current.data.permissions : null,
	);

	// THE WALK the canvas steps through, named by the address (`?pass=`, its filter beside it): the reader's own claims
	// (labeling, the default), the labeled work a browse filtered (read-only), or the work waiting for this reviewer.
	// The annotator filters it and answers the page around the open item, and where that page sits in the whole walk.
	const pass = $derived(readPass(page.url.searchParams));
	const passQuery = $derived(readItemQuery(page.url.searchParams));
	const WALK_PAGE = 200;
	const walkRead = $derived(
		pageTasks({
			projectId,
			filter: passParams(pass, passQuery, me).toString(),
			limit: WALK_PAGE,
			around: taskId,
		}),
	);
	const walkListing = $derived(walkRead.current?.ok ? walkRead.current.data : null);
	const walkItems = $derived(
		pass === 'label'
			? claimedStream(walkListing?.details ?? [], me, data.authEnabled)
			: (walkListing?.details ?? []),
	);
	const passFilter = $derived(writeItemQuery({ ...passQuery, page: 1 }, new URLSearchParams()));
	// A review pass counts where the reviewer is in it (`readPassCount`): how many waited when it started, and how many
	// they have judged since, carried from item to item in the address.
	const passCount = $derived(readPassCount(page.url.searchParams));
	const hrefWith =
		(done: number) =>
		(t: StreamTask, project: string): string =>
			pass === 'label'
				? taskCanvasHref(t, project)
				: passHref(
						t,
						project,
						pass,
						pass === 'review' ? withPassCount(passFilter, { ...passCount, done }) : passFilter,
					);
	const hrefOf = (t: StreamTask, project: string): string => hrefWith(passCount.done)(t, project);
	const stream = $derived(
		streamPosition(
			walkItems,
			taskId,
			projectId,
			link,
			pass === 'review'
				? passWindow(passCount, walkListing?.offset ?? 0, walkListing?.matched ?? 0)
				: {
						offset: walkListing?.offset ?? 0,
						total: pass === 'label' ? walkItems.length : (walkListing?.matched ?? 0),
					},
			hrefOf,
		),
	);
	/** What the walk's controls are called: the arrows over the canvas, and the strip of its items. */
	const WALK_LABELS = {
		label: { nav: 'Your labeling queue', strip: 'Your claimed items' },
		browse: { nav: 'Browsing', strip: 'The items you browse' },
		review: { nav: 'Reviewing', strip: 'The items waiting for your review' },
	} as const;
	const reviewDoneHref = $derived(link(`/projects/${encodeURIComponent(projectId)}?tab=review`));
	const readOnlyWhy = $derived(
		task && permissions
			? pass === 'browse'
				? 'Browsing: the canvas is read-only.'
				: readOnlyReason(task, permissions, me, task.people)
			: null,
	);

	const unitKey = $derived(task?.source.keys[0] ?? null);
	const dataset = $derived(task?.source.where ?? null);
	const kind = $derived(task?.media.kind ?? 'image');
	const temporal = $derived(kind === 'audio' || kind === 'video');
	const textual = $derived(kind === 'text');
	const rowKey = $derived(unitKey === null ? null : rowKeyFor([unitKey]));

	// The item's own row and its dataset's declaration: its name, and on a recording or a video its own stretch.
	const viewRead = $derived(dataset ? datasetView(dataset) : null);
	const view = $derived(viewRead?.current ?? null);
	const rowsRead = $derived(dataset && rowKey ? corpusRows({ dataset, keys: [rowKey] }) : null);
	const row = $derived(rowsRead?.current?.rows[0] ?? null);
	const partsRead = $derived(
		dataset && rowKey ? documentParts({ dataset, docId: rowKey.doc_id }) : null,
	);
	const docParts = $derived(partsRead?.current?.ok ? partsRead.current.data : []);
	const turnsRead = $derived(
		dataset && rowKey && temporal ? documentTurns({ dataset, docId: rowKey.doc_id }) : null,
	);
	const name = $derived(view && row ? itemName(view, row) : (unitKey ?? taskId));

	const projectRead = $derived(task && !elsewhere ? fetchProject({ projectId }) : null);
	const projectName = $derived(
		projectRead?.current?.ok
			? projectRead.current.data.project.title || projectRead.current.data.project.slug
			: null,
	);
	// The breadcrumb names the project and the item, each by its path; the ids stay as their tooltips.
	$effect(() => {
		if (!projectName) return;
		return nameCrumb(`/projects/${encodeURIComponent(projectId)}`, {
			label: projectName,
			title: projectId,
		});
	});
	$effect(() => {
		if (!view || !row) return;
		return nameCrumb(
			`/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`,
			{ label: name, title: `task ${taskId} · ${unitKey ?? ''}` },
		);
	});

	/** On a recording or a video: its parts, its speakers and the item's own stretch, on the canvas's time line. */
	const timeParts = $derived.by<Part[]>(() => {
		if (!view || !temporal) return [];
		const out: Part[] = [];
		const own = view && row ? view.time(row) : null;
		for (const p of docParts) {
			const t = view.time(p);
			if (t)
				out.push({
					start: t.start,
					end: t.end,
					text: view.body(p),
					own: own !== null && t.start === own.start && t.end === own.end,
				});
		}
		return out;
	});
	const focus = $derived(view && row ? view.time(row) : null);

	/** The unit's picture: a frame (or a document's thumbnail) signed for this reader; `signing` signs it afresh. */
	let signing = $state(0);
	const picture = $derived.by(() => {
		const fresh = signing > 0;
		// A text is its row's own text: nothing to sign.
		if (!dataset || unitKey === null || textual) return null;
		const key = rowKeyFor([unitKey]);
		if (key === null) return null;
		// A recording or a video plays its whole document; a picture is the unit's own frame.
		const item: MediaItem = temporal
			? { role: 'media', doc_id: key.doc_id }
			: key.keys.length === 0
				? { role: 'thumbnail', doc_id: key.doc_id }
				: { role: 'frame', doc_id: key.doc_id, keys: key.keys, frame_idx: 0 };
		// Untracked: the loader's held links are reactive, and their answer must not re-run this and sign again.
		return untrack(() => mediaLinks.resolve(dataset, item, fresh));
	});

	function mediaOf(url: string): CanvasMedia {
		if (!temporal || !rowKey || !dataset) return { kind: 'image', url };
		return {
			kind: kind === 'video' ? 'video' : 'audio',
			url,
			dataset,
			docId: rowKey.doc_id,
			parts: timeParts,
			turns: turnsRead?.current?.ok ? turnsRead.current.data : [],
			focus,
		};
	}

	// The item's other replicas (its consensus group), whose versions the canvas compares with its own.
	const groupRead = $derived(
		task?.replica_of
			? pageTasks({
					projectId,
					filter: new URLSearchParams({ group: task.replica_of }).toString(),
					limit: 10,
				})
			: null,
	);
	const replicas = $derived(
		(groupRead?.current?.ok ? (groupRead.current.data.details ?? []) : []).filter(
			(t) => t.task_id !== task?.task_id,
		),
	);

	// A 401 is a session that ended: the reader signs in again and comes back here, once. Refused again right after,
	// the page says so rather than sending them round in a loop.
	const returnTo = $derived(page.url.pathname + page.url.search);
	let signInFailed = $state(false);
	$effect(() => {
		if (!answer) return;
		if (answer.ok) signedInAt(returnTo);
		else if (answer.status === 401) signInFailed = !signInAgainOnce(returnTo);
	});
</script>

<!-- The tab names the item once it is read, never by its ids: a screen reader announces the title on arrival. -->
<svelte:head>
	<title
		>{view && row
			? titleOf(name, projectName ?? 'Projects')
			: titleOf(projectName ?? 'Projects')}</title
	>
</svelte:head>

{#snippet shell(media: CanvasMedia)}
	{#if task && dataset && unitKey !== null && permissions !== null}
		{#key task.task_id}
			<CanvasShell
				{task}
				{unitKey}
				{media}
				itemName={name}
				{permissions}
				{me}
				{ontaskchange}
				annotationsUrl={annotationsUrl({
					project: task.project_id,
					task: task.task_id,
					dataset,
					key: unitKey,
				})}
				readOnly={readOnlyWhy}
				person={me ?? 'anonymous'}
				{stream}
				walk={{
					items: walkItems,
					offset: walkListing?.offset ?? 0,
					total: stream.total,
					hrefOf,
					label: WALK_LABELS[pass].nav,
					stripLabel: WALK_LABELS[pass].strip,
					browsing: pass === 'browse',
				}}
				{exitHref}
				{link}
				{replicas}
			/>
		{/key}
	{/if}
{/snippet}

<h1 class="sr-only">Label {name}</h1>
{#if gpuReason}
	<div
		class="text-muted-foreground flex flex-col items-center justify-center gap-1 p-6 text-center text-sm"
		data-testid="canvas-gpu-refusal"
	>
		<span class="text-destructive">{gpuReason}</span>
		<span>The labeling canvas draws with WebGPU. The project's queue works without it.</span>
		<!-- eslint-disable-next-line svelte/no-navigation-without-resolve -->
		<a class="mt-2 underline underline-offset-2" href={exitHref}>Back to the project</a>
	</div>
{:else if answer === undefined || gpuReason === undefined}
	<p class="text-muted-foreground p-6 text-sm" role="status">Reading the item…</p>
{:else if !answer.ok && answer.status === 401}
	<p class="text-muted-foreground p-6 text-sm" role="status" data-testid="task-signin">
		{signInFailed
			? 'We could not sign you in: reload the page to try again.'
			: 'Your session ended: signing you in again…'}
	</p>
{:else if !answer.ok && (answer.status === 403 || answer.status === 404)}
	<div class="flex flex-col gap-1 p-6 text-sm" data-testid="task-unseen">
		<p class="text-lg font-semibold">No task {taskId} that you can see</p>
		<p class="text-muted-foreground">It does not exist, or you hold no role on its project.</p>
		<!-- eslint-disable-next-line svelte/no-navigation-without-resolve -->
		<a class="mt-2 w-fit underline underline-offset-2" href={exitHref}>Back to the project</a>
	</div>
{:else if !answer.ok}
	<LoadFailure
		what="The item could not be read."
		detail={answer.detail}
		hint={ANNOTATOR_HINT}
		onretry={() => void taskRead.refresh()}
	/>
{:else if task && !elsewhere}
	{#if kind !== 'image' && kind !== 'audio' && kind !== 'video' && kind !== 'text'}
		<p class="p-6 text-sm" data-testid="canvas-unsupported">
			This item is {kind}; its canvas is not in this app yet.
		</p>
	{:else if textual && (!view || !rowsRead?.current)}
		<p class="text-muted-foreground p-6 text-sm" role="status">Reading the item's text…</p>
	{:else if textual && permissions === null}
		<p class="text-muted-foreground p-6 text-sm" role="status">Reading your role on the project…</p>
	{:else if textual && dataset && unitKey !== null}
		{@render shell({ kind: 'text', text: view && row ? view.body(row) : '' })}
	{:else if temporal && (!view || !partsRead?.current || (turnsRead && !turnsRead.current))}
		<p class="text-muted-foreground p-6 text-sm" role="status">
			Reading the {kind === 'video' ? 'video' : 'recording'}'s time line…
		</p>
	{:else if !dataset || unitKey === null || picture === null}
		<p class="p-6 text-sm" data-testid="canvas-unsupported">
			This item names no dataset row to label.
		</p>
	{:else if permissions === null}
		<p class="text-muted-foreground p-6 text-sm" role="status">Reading your role on the project…</p>
	{:else}
		{#await picture}
			<p class="text-muted-foreground p-6 text-sm" role="status">
				Signing the {temporal ? kind : 'picture'}…
			</p>
		{:then signed}
			{#if signed.url === null}
				<LoadFailure
					what={`The ${temporal ? kind : 'picture'} could not be signed.`}
					detail={signed.detail}
					hint="The viewer signs the media: check that it is running and that you may read this dataset."
					onretry={() => (signing += 1)}
				/>
			{:else}
				{#if task.source.keys.length > 1}
					<p class="border-b px-3 py-1 text-xs" role="note" data-testid="canvas-one-unit">
						This item names {task.source.keys.length} units. A task's draft holds one unit's shapes, so
						the canvas opens its first: {name}.
					</p>
				{/if}
				{@render shell(mediaOf(signed.url))}
			{/if}
		{/await}
	{/if}
{/if}
