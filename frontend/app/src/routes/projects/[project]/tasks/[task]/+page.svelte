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
	 * opens that unit and says so when the item names more.
	 *
	 * WHO EDITS. The reader's permissions on the project and the task's state and holder decide whether the canvas takes
	 * edits (`readOnlyReason`); otherwise it opens read-only and says why. The doors decide on their own.
	 */
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { linkWith } from '#lib/project.ts';
	import { titleOf } from '#lib/nav.ts';
	import { annotationsUrl } from '#lib/api/annotations.api.ts';
	import { mediaLinks } from '#lib/api/media.api.ts';
	import type { MediaItem } from '#lib/api/media-links.svelte.ts';
	import { fetchProject, fetchTask, listTasks } from '#lib/api/projects.api.ts';
	import { gpuUnsupportedReason } from '#lib/gpu/gpu-support.ts';
	import { rowKeyFor } from '#lib/projects/corpus-rows.ts';
	import { ANNOTATOR_HINT } from '#lib/projects/presentation.ts';
	import { signInAgainOnce, signedInAt } from '#lib/reauth.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import CanvasShell from '#lib/canvas/CanvasShell.svelte';
	import { readOnlyReason } from '#lib/canvas/editability.ts';
	import { claimedStream, streamPosition, taskCanvasHref } from '#lib/canvas/task-stream.ts';

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
	const task = $derived(answer?.ok ? answer.data : null);
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

	// The label stream: the reader's claimed items in this project, in the queue's order.
	const listingRead = $derived(listTasks({ projectId, details: true }));
	const claimed = $derived(
		claimedStream(
			listingRead.current?.ok ? (listingRead.current.data.details ?? []) : [],
			me,
			data.authEnabled,
		),
	);
	const stream = $derived(streamPosition(claimed, taskId, projectId, link));

	const unitKey = $derived(task?.source.keys[0] ?? null);
	const dataset = $derived(task?.source.where ?? null);

	/** The unit's picture: a frame (or a document's thumbnail) signed for this reader; `signing` signs it afresh. */
	let signing = $state(0);
	const picture = $derived.by(() => {
		const fresh = signing > 0;
		if (!dataset || unitKey === null) return null;
		const key = rowKeyFor([unitKey]);
		if (key === null) return null;
		const item: MediaItem =
			key.keys.length === 0
				? { role: 'thumbnail', doc_id: key.doc_id }
				: { role: 'frame', doc_id: key.doc_id, keys: key.keys, frame_idx: 0 };
		// Untracked: the loader's held links are reactive, and their answer must not re-run this and sign again.
		return untrack(() => mediaLinks.resolve(dataset, item, fresh));
	});

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

<svelte:head><title>{titleOf(taskId, 'Projects')}</title></svelte:head>

<h1 class="sr-only">Label {taskId}</h1>
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
	{#if task.media.kind !== 'image'}
		<p class="p-6 text-sm" data-testid="canvas-unsupported">
			This item is {task.media.kind}; its canvas is not in this app yet.
		</p>
	{:else if !dataset || unitKey === null || picture === null}
		<p class="p-6 text-sm" data-testid="canvas-unsupported">
			This item names no dataset row to label.
		</p>
	{:else if permissions === null}
		<p class="text-muted-foreground p-6 text-sm" role="status">Reading your role on the project…</p>
	{:else}
		{#await picture}
			<p class="text-muted-foreground p-6 text-sm" role="status">Signing the picture…</p>
		{:then signed}
			{#if signed.url === null}
				<LoadFailure
					what="The picture could not be signed."
					detail={signed.detail}
					hint="The viewer signs the pictures: check that it is running and that you may read this dataset."
					onretry={() => (signing += 1)}
				/>
			{:else}
				{#if task.source.keys.length > 1}
					<p class="border-b px-3 py-1 text-xs" role="note" data-testid="canvas-one-unit">
						This item names {task.source.keys.length} units. A task's draft holds one unit's shapes, so
						the canvas opens its first: {unitKey}.
					</p>
				{/if}
				{#key task.task_id}
					<CanvasShell
						{task}
						{unitKey}
						imageUrl={signed.url}
						annotationsUrl={annotationsUrl({
							project: task.project_id,
							task: task.task_id,
							dataset,
							key: unitKey,
						})}
						readOnly={readOnlyReason(task, permissions, me)}
						person={me ?? 'anonymous'}
						{stream}
						{claimed}
						{exitHref}
						{link}
					/>
				{/key}
			{/if}
		{/await}
	{/if}
{/if}
