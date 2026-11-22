<script lang="ts">
	/**
	 * One project, in six tabs whose state is the address (`?tab=`, each tab's filters beside it): its items (the data
	 * manager, `DataManager.svelte`), labelling many of the items open to the reader at once (the grid, `LabelGrid.svelte`:
	 * a tab of its own, because it lists the reader's own work rather than the project's, and its keys are its own), browsing its labeled work, reviewing what waits for the reader, its settings
	 * (what is labeled, the review and claim rules, who holds which role) and its publish. The annotator filters, counts
	 * and pages every list; the page reads the project and each person's part over all its items, and reads them again
	 * after every action.
	 *
	 * What the reader may do comes with the project's read (`permissions`, from the workbench's store): an action the
	 * reader's role does not allow is not offered, and the door refuses it regardless.
	 */
	import { page } from '$app/state';
	import { linkWith } from '#lib/project.ts';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import * as Tabs from '@riksarkivet/ui/tabs';
	import { ArrowLeft, Inbox, Pencil, RefreshCw } from '@lucide/svelte';
	import { fetchProject, fireProjectEvent, pageTasks } from '#lib/api/projects.api.ts';
	import { goto } from '$app/navigation';
	import AdjudicationPanel from '#lib/projects/AdjudicationPanel.svelte';
	import AnnotatorMetrics from '#lib/projects/AnnotatorMetrics.svelte';
	import EditOntologyDialog from '#lib/projects/EditOntologyDialog.svelte';
	import MembersPanel from '#lib/projects/MembersPanel.svelte';
	import PublishPanel from '#lib/projects/PublishPanel.svelte';
	import SendItemsDialog from '#lib/projects/SendItemsDialog.svelte';
	import DataManager from '#lib/projects/DataManager.svelte';
	import LabelGrid from '#lib/projects/LabelGrid.svelte';
	import BrowsePanel from '#lib/projects/BrowsePanel.svelte';
	import ReviewPanel from '#lib/projects/ReviewPanel.svelte';
	import SettingsPanel from '#lib/projects/SettingsPanel.svelte';
	import {
		ANNOTATOR_HINT,
		EVENT_LABELS,
		projectStateVariant,
		roleSummary,
	} from '#lib/projects/presentation.ts';
	import type { LegalEvent } from '#lib/projects/types.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import { titleOf } from '#lib/nav.ts';
	import { nameCrumb } from '#lib/shell/crumb-names.svelte.ts';

	let { data } = $props();

	const projectId = $derived(page.params.project ?? '');
	const me = $derived(data.me?.sub ?? null);

	const detailRead = $derived(fetchProject({ projectId }));
	// The project's index and each person's part over every item: the counts and names the tabs share. Each tab reads
	// its own page of items.
	const contributorsRead = $derived(pageTasks({ projectId, details: false, contributors: true }));
	const answer = $derived(detailRead.current);
	const detail = $derived(answer?.ok ? answer.data : null);
	const contributors = $derived(
		contributorsRead.current?.ok ? contributorsRead.current.data : null,
	);
	const held = $derived(new Set(detail?.permissions ?? []));
	const classNames = $derived((detail?.project.ontology.classes ?? []).map((c) => c.name));

	// The open tab's panel, which holds its own reads (a page of items, a list), read again with the page's.
	let manager = $state<DataManager | undefined>();
	let labeler = $state<LabelGrid | undefined>();
	let browser = $state<BrowsePanel | undefined>();
	let reviewer = $state<ReviewPanel | undefined>();

	/** Read again everything the page shows: the project, each person's part, and the open tab's own reads. */
	function refresh(): void {
		void detailRead.refresh();
		void contributorsRead.refresh();
		manager?.reload();
		labeler?.reload();
		browser?.reload();
		reviewer?.reload();
	}

	// Coming back to the window (its focus, or the browser tab shown again) reads the page again, so what another
	// person did meanwhile is not shown stale; a burst of both events reads once. Live updates are a later step.
	let returnedAt = 0;
	function returned(): void {
		if (document.visibilityState !== 'visible' || Date.now() - returnedAt < 1000) return;
		returnedAt = Date.now();
		refresh();
	}

	// THE ADDRESS IS THE VIEW: the tab is `?tab=`, and each tab's filters ride beside it (`item-query.ts`). Changing tab
	// keeps the project and leaves the other tab's filters behind.
	const TABS = ['items', 'label', 'browse', 'review', 'settings', 'publish'] as const;
	type Tab = (typeof TABS)[number];
	const tab = $derived<Tab>(
		(TABS as readonly string[]).includes(page.url.searchParams.get('tab') ?? '')
			? (page.url.searchParams.get('tab') as Tab)
			: 'items',
	);
	function openTab(next: Tab): void {
		const address = new URLSearchParams();
		const project = page.url.searchParams.get('project');
		if (project !== null) address.set('project', project);
		if (next !== 'items') address.set('tab', next);
		// oxlint-disable-next-line svelte/no-navigation-without-resolve -- the same path, its query changed
		void goto(`?${address}`, { reset: false });
	}
	const TAB_CLASS =
		'flex min-w-0 flex-col gap-4 focus-visible:ring-ring/50 rounded-md outline-none focus-visible:ring-3';

	// Past `frozen` a publish is being prepared against the answer set, so nothing may still change what this project
	// will publish: editing the ontology and removing an item are both that. The doors enforce each on their own.
	const stillMutable = $derived(
		detail?.project.state === 'draft' || detail?.project.state === 'labeling',
	);

	// The publish saga narrates its steps onto the project document, which moves no other signal: while the project is
	// publishing, it is read again every 2 s, and not at all once it settles.
	$effect(() => {
		if (detail?.project.state !== 'publishing') return;
		const timer = setInterval(refresh, 2000);
		return () => clearInterval(timer);
	});

	/** The header's transitions: what the project's state allows and the reader's role permits, except publish (the
	 *  Publish tab confirms it) and send (its dialog takes the items). */
	const legal = $derived(
		(detail?.legal_events ?? []).filter((e: LegalEvent) => held.has(e.permission)),
	);
	const headerEvents = $derived(legal.filter((e) => e.event !== 'publish' && e.event !== 'send'));
	const canSend = $derived(legal.some((e) => e.event === 'send'));
	const canPublish = $derived((detail?.legal_events ?? []).some((e) => e.event === 'publish'));

	let notice = $state('');
	let sendOpen = $state(false);
	let ontologyOpen = $state(false);
	let firing = $state(false);

	async function fireProject(event: string): Promise<void> {
		if (firing) return;
		firing = true;
		notice = '';
		const result = await fireProjectEvent({ projectId, event });
		firing = false;
		if (result.ok) refresh();
		// The door's words: a 403 names the missing role, a 409 the illegal edge.
		else notice = result.detail;
	}

	const name = $derived(detail ? detail.project.title || detail.project.slug : projectId);
	// The breadcrumb names the project by its title, its id the tooltip.
	$effect(() => {
		if (!detail) return;
		return nameCrumb(`/projects/${encodeURIComponent(projectId)}`, {
			label: name,
			title: projectId,
		});
	});
	// Back keeps this project as the reader's (`?project=`), as every in-app link does.
	const back = $derived(linkWith(projectId)('/projects'));
</script>

<svelte:window onfocus={returned} />
<svelte:document onvisibilitychange={returned} />

<!-- The tab names the project once it is read, never by its id: a screen reader announces the title on arrival. -->
<svelte:head><title>{detail ? titleOf(name, 'Projects') : titleOf('Projects')}</title></svelte:head>

{#key projectId}
	{#if answer === undefined}
		<h1 class="sr-only">{projectId}</h1>
		<p class="text-muted-foreground p-6 text-sm" role="status">Reading the project…</p>
	{:else if !answer.ok && answer.status === 401}
		<h1 class="sr-only">{projectId}</h1>
		<!-- A 401 is a session that ended, not a project that is hidden: the shell's one prompt asks to sign in again. -->
		<p class="text-muted-foreground p-6 text-sm" role="status" data-testid="project-signin">
			Your session ended: sign in again to read the project.
		</p>
	{:else if !answer.ok && (answer.status === 403 || answer.status === 404)}
		<!-- A 403 and a 404 read the same: the annotator keeps a project's existence to those who may view it. -->
		<div class="flex flex-col gap-1 p-6 text-sm" data-testid="project-unseen">
			<h1 class="text-lg font-semibold">No project {projectId} that you can see</h1>
			<p class="text-muted-foreground">It does not exist, or you hold no role on it.</p>
			<p class="text-muted-foreground break-words">Details: {answer.detail}</p>
			<!-- `back` resolves the path through `linkWith`, with the reader's project after it. -->
			<!-- eslint-disable-next-line svelte/no-navigation-without-resolve -->
			<a class="mt-2 w-fit underline underline-offset-2" href={back}>Back to the projects</a>
		</div>
	{:else if !answer.ok}
		<h1 class="sr-only">{projectId}</h1>
		<LoadFailure
			what="The project could not be read."
			detail={answer.detail}
			hint={ANNOTATOR_HINT}
			onretry={refresh}
		/>
	{:else if detail}
		{@const project = detail.project}
		<div class="flex w-full min-w-0 flex-col gap-4 overflow-y-auto p-4 sm:p-6">
			<header class="flex flex-wrap items-start justify-between gap-3">
				<div class="flex min-w-0 items-start gap-2">
					<Button variant="ghost" size="icon-sm" href={back} aria-label="Back to the projects">
						<ArrowLeft class="size-4" />
					</Button>
					<div class="min-w-0">
						<h1 class="flex flex-wrap items-center gap-2 text-2xl font-semibold break-words">
							{name}
							<Badge variant={projectStateVariant(project.state)}
								>{project.state.replace('_', ' ')}</Badge
							>
						</h1>
						<p class="text-muted-foreground font-mono text-xs break-all">
							{project.tenant} / {project.slug}
						</p>
						<p class="text-muted-foreground text-xs" data-testid="reader-role">
							{roleSummary(detail.permissions)}
						</p>
					</div>
				</div>
				<div class="flex flex-wrap items-center gap-2">
					<Button variant="outline" size="sm" onclick={refresh}>
						<RefreshCw class="size-3.5" /> Refresh
					</Button>
					{#if canSend}
						<Button variant="outline" size="sm" onclick={() => (sendOpen = true)}>
							<Inbox class="size-3.5" />
							{EVENT_LABELS['send']}
						</Button>
					{/if}
					{#each headerEvents as event (event.event)}
						<Button
							variant="outline"
							size="sm"
							disabled={firing}
							onclick={() => void fireProject(event.event)}
						>
							{EVENT_LABELS[event.event] ?? event.event}
						</Button>
					{/each}
				</div>
			</header>

			{#if project.description}
				<p class="text-muted-foreground max-w-3xl text-sm">{project.description}</p>
			{/if}
			{#if project.instructions}
				<div
					class="bg-muted/40 max-w-3xl rounded-md border px-3 py-2 text-sm"
					data-testid="instructions"
				>
					<h2 class="text-muted-foreground text-xs font-medium uppercase">Instructions</h2>
					<p class="mt-1 whitespace-pre-line">{project.instructions}</p>
				</div>
			{/if}
			{#if notice}
				<p class="text-destructive text-sm break-words" role="alert">{notice}</p>
			{/if}

			<Tabs.Root value={tab} onValueChange={(v) => openTab(v as Tab)}>
				<Tabs.List class="max-w-full overflow-x-auto">
					<Tabs.Trigger value="items" data-testid="tab-items">Items</Tabs.Trigger>
					<Tabs.Trigger value="label" data-testid="tab-label">Label</Tabs.Trigger>
					<Tabs.Trigger value="browse" data-testid="tab-browse">Browse</Tabs.Trigger>
					<Tabs.Trigger value="review" data-testid="tab-review">Review</Tabs.Trigger>
					<Tabs.Trigger value="settings" data-testid="tab-settings">Settings</Tabs.Trigger>
					<Tabs.Trigger value="publish" data-testid="tab-publish">Publish</Tabs.Trigger>
				</Tabs.List>

				<Tabs.Content value="items" class={TAB_CLASS}>
					{#if tab === 'items'}
						<AnnotatorMetrics
							rows={contributors?.contributors ?? []}
							people={contributors?.people ?? {}}
						/>
						<DataManager
							bind:this={manager}
							{projectId}
							permissions={detail.permissions}
							droppable={stillMutable && held.has('can_manage')}
							classes={classNames}
							contributors={contributors?.contributors ?? []}
							contributorPeople={contributors?.people ?? {}}
							{me}
							consensusN={project.consensus_n ?? 1}
							adjudications={project.adjudications ?? {}}
							sampling={project.sampling}
							onchanged={refresh}
						/>
					{/if}
				</Tabs.Content>

				<Tabs.Content value="label" class={TAB_CLASS}>
					{#if tab === 'label'}
						<LabelGrid
							bind:this={labeler}
							{projectId}
							permissions={detail.permissions}
							{me}
							classes={project.ontology.classes}
							sampling={project.sampling}
							onchanged={refresh}
						/>
					{/if}
				</Tabs.Content>

				<Tabs.Content value="browse" class={TAB_CLASS}>
					{#if tab === 'browse'}
						<BrowsePanel
							bind:this={browser}
							{projectId}
							permissions={detail.permissions}
							{me}
							classes={classNames}
							consensusN={project.consensus_n ?? 1}
						/>
					{/if}
				</Tabs.Content>

				<Tabs.Content value="review" class={TAB_CLASS}>
					{#if tab === 'review'}
						<ReviewPanel
							bind:this={reviewer}
							{projectId}
							permissions={detail.permissions}
							{me}
							consensusN={project.consensus_n ?? 1}
						/>
					{/if}
				</Tabs.Content>

				<Tabs.Content value="settings" class="{TAB_CLASS} max-w-3xl">
					{#if tab === 'settings'}
						<SettingsPanel
							{project}
							mayManage={held.has('can_manage')}
							holdsItems={(contributors?.total ?? 0) > 0}
							onsaved={refresh}
						/>
						<section class="flex flex-col gap-2 border-t pt-3" data-testid="settings-labels">
							<h2 class="text-muted-foreground text-xs font-medium uppercase">Labels</h2>
							{#if project.ontology.kind}
								<div class="flex items-center gap-1.5 text-sm" data-testid="task-kind-chip">
									<span class="text-muted-foreground text-xs">Task type:</span>
									<Badge>{project.ontology.kind}</Badge>
								</div>
							{/if}
							{#if project.ontology.classes.length > 0}
								<div
									class="flex flex-wrap items-center gap-1.5 text-sm"
									data-testid="label-taxonomy"
								>
									{#each project.ontology.classes as labelClass (labelClass.name)}
										<Badge variant={labelClass.required ? 'default' : 'outline'}>
											{labelClass.name}
											{#if labelClass.tools.length > 0}
												<span class={labelClass.required ? '' : 'text-muted-foreground'}
													>· {labelClass.tools.join('/')}</span
												>
											{/if}
											{#if labelClass.required}
												<span>· required</span>
											{/if}
										</Badge>
									{/each}
								</div>
							{:else}
								<p class="text-muted-foreground text-sm">
									No labels are defined: any label may be drawn.
								</p>
							{/if}
							{#if project.ontology.relations.length > 0}
								<div class="flex flex-wrap items-center gap-1.5 text-sm" data-testid="relations">
									<span class="text-muted-foreground text-xs">Relations:</span>
									{#each project.ontology.relations as rel (rel.name)}
										<Badge variant="outline">{rel.name}</Badge>
									{/each}
								</div>
							{/if}
							{#if stillMutable && held.has('can_manage')}
								<div>
									<Button
										variant="outline"
										size="sm"
										data-testid="edit-ontology-trigger"
										onclick={() => (ontologyOpen = true)}
									>
										<Pencil class="size-3.5" />
										{project.ontology.classes.length > 0 ? 'Edit the labels' : 'Define the labels'}
									</Button>
								</div>
							{/if}
							<p class="text-muted-foreground text-xs">
								The labels change while the project takes work (draft or labeling). Items nobody has
								started take the new labels; an item someone has claimed, drawn on or submitted
								keeps the labels it was sent with, so its work is judged by them.
							</p>
						</section>
						<section class="border-t pt-3">
							{#if held.has('can_manage')}
								<MembersPanel {projectId} />
							{:else}
								<p class="text-muted-foreground text-sm" data-testid="members-not-shown">
									Who holds a role on this project is shown to its managers.
								</p>
							{/if}
						</section>
					{/if}
				</Tabs.Content>

				<Tabs.Content value="publish" class="{TAB_CLASS} max-w-3xl">
					{#if tab === 'publish'}
						<PublishPanel
							{project}
							listing={contributors}
							contributors={contributors?.contributors ?? []}
							people={contributors?.people ?? {}}
							{canPublish}
							mayPublish={held.has('can_publish')}
							onchanged={refresh}
						/>
						<AdjudicationPanel
							{projectId}
							adjudications={project.adjudications ?? {}}
							mayManage={held.has('can_manage')}
							onchanged={refresh}
						/>
					{/if}
				</Tabs.Content>
			</Tabs.Root>
		</div>

		<SendItemsDialog {projectId} bind:open={sendOpen} onsent={refresh} />
		<EditOntologyDialog
			{projectId}
			ontology={project.ontology}
			bind:open={ontologyOpen}
			onsaved={refresh}
		/>
	{/if}
{/key}
