<script lang="ts">
	/**
	 * The datasets the lakehouse shows the reader, each a link to its page. The viewer lists them on the
	 * reader's own read, so a dataset the lakehouse does not let them read is not here.
	 */
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import { Button } from '@workbench/ui/button';
	import { fetchDatasets } from '#lib/api/datasets.api.ts';
	import { linkWith, projectOf } from '#lib/project.ts';
	import { titleOf } from '#lib/nav.ts';

	let { data } = $props();

	const project = $derived(projectOf(page.url));
	/** A dataset's page, by its route id, with the reader's project kept (`?project=`). */
	const pageOf = (id: string): string =>
		`${resolve('/datasets/[id]', { id: encodeURIComponent(id) })}${projectQuery}`;

	// The project rides every link (`?project=`).
	const projectQuery = $derived(project === null ? '' : `?${new URLSearchParams({ project })}`);
	const datasets = $derived(fetchDatasets());
	const projects = $derived(data.me?.projects.map((p) => p.project) ?? []);

	const rows = (tables: Record<string, { row_count: number }>): number =>
		Object.values(tables).reduce((sum, t) => sum + t.row_count, 0);

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
			<p class="text-muted-foreground text-sm">What the lakehouse lets you read.</p>
		</div>
		<label class="flex items-center gap-2 text-sm">
			<span class="text-muted-foreground">Project</span>
			<select
				class="border-input bg-background focus-visible:ring-ring/50 h-8 rounded-lg border px-2 text-sm outline-none focus-visible:ring-3"
				value={project ?? ''}
				onchange={chooseProject}
			>
				<option value="">No project</option>
				{#each projects as id (id)}
					<option value={id}>{id}</option>
				{/each}
				{#if project !== null && !projects.includes(project)}
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
	{:else if datasets.current.datasets.length === 0}
		<div class="text-muted-foreground flex flex-col gap-1 text-sm">
			<p>The lakehouse shows you no dataset.</p>
			<p>A dataset is listed once the lakehouse grants you a read on it.</p>
		</div>
	{:else}
		<ul class="grid gap-3 sm:grid-cols-2" aria-label="Datasets">
			{#each datasets.current.datasets as dataset (dataset.id)}
				<li>
					<!-- The route id resolved by `pageOf`, with the reader's ?project= after it, which the rule cannot see. -->
					<!-- eslint-disable svelte/no-navigation-without-resolve -->
					<a
						href={pageOf(dataset.id)}
						class="hover:bg-accent focus-visible:ring-ring/50 flex flex-col gap-1 rounded-xl border p-4 outline-none focus-visible:ring-3"
					>
						<span class="font-mono text-sm font-medium break-all">{dataset.id}</span>
						<span class="text-muted-foreground text-xs">
							{Object.keys(dataset.tables).length} tables · {rows(dataset.tables).toLocaleString()} rows
						</span>
						{#if dataset.capabilities.length > 0}
							<span class="flex flex-wrap gap-1">
								{#each dataset.capabilities as capability (capability)}
									<span class="bg-muted rounded px-1.5 py-0.5 text-xs">{capability}</span>
								{/each}
							</span>
						{/if}
					</a>
					<!-- eslint-enable svelte/no-navigation-without-resolve -->
				</li>
			{/each}
		</ul>
	{/if}
</div>
