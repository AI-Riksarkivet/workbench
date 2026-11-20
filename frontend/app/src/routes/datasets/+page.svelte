<script lang="ts">
	/**
	 * The datasets the deployment's sources show the reader, grouped by source, each a link to its page.
	 * A lakehouse lists them on the reader's own read, so a dataset it does not let them read is not here;
	 * any other source lists what its catalog holds. A dataset that carries a workbench descriptor is a
	 * media dataset; one that carries none is a plain table, shown from its schema. What could not be
	 * listed or opened is listed too, with the reason.
	 */
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { Button } from '@riksarkivet/ui/button';
	import { fetchDatasets } from '#lib/api/datasets.api.ts';
	import { listProjects } from '#lib/api/projects.api.ts';
	import { linkWith, projectOf } from '#lib/project.ts';
	import { titleOf } from '#lib/nav.ts';
	import { datasetParts } from '#lib/dataset-label.ts';

	let { data } = $props();

	const project = $derived(projectOf(page.url));
	/** A dataset's page, by its route id, with the reader's project kept (`?project=`). */
	const pageOf = (id: string): string =>
		`${resolve('/datasets/[id]', { id: encodeURIComponent(id) })}${projectQuery}`;

	// The project rides every link (`?project=`).
	const projectQuery = $derived(project === null ? '' : `?${new URLSearchParams({ project })}`);
	const datasets = $derived(fetchDatasets());
	// The projects the reader can see, across their organisations: the one chosen is what a send from this
	// dataset's search or atlas goes into, and what every link keeps (`?project=`).
	const projectReads = $derived((data.me?.orgs ?? []).map((o) => listProjects({ tenant: o.org })));
	const projects = $derived(
		projectReads.flatMap((read) =>
			read.current?.ok
				? read.current.data.projects.map((p) => ({ id: p.project_id, name: p.title || p.slug }))
				: [],
		),
	);

	const rows = (tables: Record<string, { row_count: number }>): number =>
		Object.values(tables).reduce((sum, t) => sum + t.row_count, 0);

	const counted = (n: number, noun: string): string =>
		`${n.toLocaleString()} ${noun}${n === 1 ? '' : 's'}`;

	/** A dataset's name within its source: its namespace, or, at the source's root, its tables. */
	const nameOf = (id: string, tables: Record<string, unknown> = {}): string =>
		datasetParts(id).namespace ?? (Object.keys(tables).sort().join(', ') || 'the root');

	const KIND_NAMES = {
		rask: 'Lakehouse',
		namespace: 'Lance namespace',
		tables: 'Lance tables',
	} as const;

	const sources = $derived(datasets.current?.sources ?? []);
	const empty = $derived(sources.every((s) => s.datasets.length === 0 && s.unlisted.length === 0));

	function chooseProject(event: Event): void {
		const value = (event.currentTarget as HTMLSelectElement).value;
		// One history entry per change, so Back is the project as it was.
		// oxlint-disable-next-line svelte/no-navigation-without-resolve -- `linkWith` resolves the path through `$app/paths`
		void goto(linkWith(value === '' ? null : value)('/datasets'), { reset: false });
	}

	const message = (err: unknown): string =>
		typeof err === 'object' && err !== null && 'body' in err
			? String((err as { body: { message?: unknown } }).body.message)
			: String(err);
</script>

<svelte:head><title>{titleOf('Datasets')}</title></svelte:head>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-4 overflow-y-auto p-4 sm:p-6">
	<header class="flex flex-wrap items-end justify-between gap-3">
		<div>
			<h1 class="text-2xl font-semibold">Datasets</h1>
			<p class="text-muted-foreground text-sm">What the deployment's sources let you read.</p>
		</div>
		<label class="flex items-center gap-2 text-sm">
			<span class="text-muted-foreground">Project</span>
			<select
				class="border-input bg-background focus-visible:ring-ring/50 h-8 rounded-lg border px-2 text-sm outline-none focus-visible:ring-3"
				value={project ?? ''}
				onchange={chooseProject}
			>
				<option value="">No project</option>
				{#each projects as p (p.id)}
					<option value={p.id}>{p.name}</option>
				{/each}
				{#if project !== null && !projects.some((p) => p.id === project)}
					<option value={project} disabled>{project} (none you belong to)</option>
				{/if}
			</select>
		</label>
	</header>

	{#if datasets.error && !datasets.current}
		<div class="flex flex-wrap items-center gap-3">
			<p class="text-destructive text-sm" role="alert">
				The datasets could not be read: {message(datasets.error)}
			</p>
			<Button variant="outline" size="sm" onclick={() => void datasets.refresh()}>Try again</Button>
		</div>
	{:else if !datasets.current}
		<p class="text-muted-foreground text-sm" role="status">Reading the datasets…</p>
	{:else if empty}
		<div class="text-muted-foreground flex flex-col gap-1 text-sm">
			<p>No source shows you a dataset.</p>
			<p>A lakehouse lists a dataset once it grants you a read on it.</p>
		</div>
	{:else}
		{#each sources as source (source.name)}
			<section class="flex flex-col gap-2" aria-labelledby="source-{source.name}">
				<header class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
					<h2 id="source-{source.name}" class="text-base font-semibold">
						{source.title || source.name}
					</h2>
					<span class="text-muted-foreground font-mono text-xs">{source.name}</span>
					<span class="bg-muted rounded px-1.5 py-0.5 text-xs">{KIND_NAMES[source.kind]}</span>
					{#if source.kind !== 'rask'}
						<span class="text-muted-foreground text-xs">read-only</span>
					{/if}
				</header>
				{#if source.datasets.length > 0}
					<ul
						class="grid gap-3 sm:grid-cols-2"
						aria-label="Datasets in {source.title || source.name}"
					>
						{#each source.datasets as dataset (dataset.id)}
							<li>
								<!-- The route id resolved by `pageOf`, with the reader's ?project= after it, which the rule cannot see. -->
								<!-- eslint-disable svelte/no-navigation-without-resolve -->
								<a
									href={pageOf(dataset.id)}
									class="hover:bg-accent focus-visible:ring-ring/50 flex h-full flex-col gap-1 rounded-xl border p-4 outline-none focus-visible:ring-3"
								>
									<span class="flex items-start justify-between gap-2">
										<span class="font-mono text-sm font-medium break-all"
											>{nameOf(dataset.id, dataset.tables)}</span
										>
										<span
											class={[
												'shrink-0 rounded px-1.5 py-0.5 text-xs',
												dataset.stamped
													? 'bg-primary/10 text-primary'
													: 'bg-muted text-muted-foreground',
											]}>{dataset.stamped ? 'media' : 'table'}</span
										>
									</span>
									<span class="text-muted-foreground text-xs">
										{counted(Object.keys(dataset.tables).length, 'table')} · {counted(
											rows(dataset.tables),
											'row',
										)}
									</span>
									{#if dataset.capabilities.length > 0}
										<span class="flex flex-wrap gap-1">
											{#each dataset.capabilities as capability (capability)}
												<span class="bg-muted rounded px-1.5 py-0.5 text-xs">{capability}</span>
											{/each}
										</span>
									{/if}
									{#if dataset.problem}
										<span class="text-muted-foreground text-xs"
											>Shown as a table: {dataset.problem}</span
										>
									{/if}
									{#each Object.entries(dataset.unreadable) as [table, reason] (table)}
										<span class="text-muted-foreground text-xs"
											><span class="font-mono">{table}</span> could not be opened: {reason}</span
										>
									{/each}
								</a>
								<!-- eslint-enable svelte/no-navigation-without-resolve -->
							</li>
						{/each}
					</ul>
				{:else if source.unlisted.length === 0}
					<p class="text-muted-foreground text-sm">This source shows you no dataset.</p>
				{/if}
				{#if source.unlisted.length > 0}
					<ul class="text-muted-foreground flex flex-col gap-0.5 text-xs" aria-label="Not listed">
						{#each source.unlisted as item (item.id)}
							<li>
								<span class="font-mono break-all">{nameOf(item.id)}</span> could not be read: {item.reason}
							</li>
						{/each}
					</ul>
				{/if}
				{#if source.truncated}
					<p class="text-muted-foreground text-xs">
						This source holds more namespaces than one listing walks; not every dataset is shown.
					</p>
				{/if}
			</section>
		{/each}
	{/if}
</div>
