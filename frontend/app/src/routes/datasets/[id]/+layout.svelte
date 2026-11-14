<script lang="ts">
	/**
	 * Every page of one dataset: its dock (`/datasets/[id]`, named by the dataset's id) and its views, the
	 * atlas, the graph and the tree (`#lib/dataset-nav.ts`), reached from one row of links. A view the
	 * dataset cannot show stays in the row, disabled, and says why when chosen, so a keyboard and a touch
	 * reach the reason as a pointer does.
	 */
	import { page } from '$app/state';
	import { resolve } from '$app/paths';
	import { datasetPages, type DatasetPage } from '#lib/dataset-nav.ts';
	import { projectOf } from '#lib/project.ts';

	let { data, children } = $props();
	const id = $derived(data.id);
	const project = $derived(projectOf(page.url));

	/** What a link inside the dataset keeps from the address: the project and the table in view. */
	const kept = $derived.by(() => {
		const params = new URLSearchParams();
		for (const name of ['project', 'table']) {
			const value = page.url.searchParams.get(name);
			if (value) params.set(name, value);
		}
		const query = params.toString();
		return query ? `?${query}` : '';
	});

	const rowTable = $derived(
		data.view?.searchTable(page.url.searchParams.get('table'))?.row_table ?? null,
	);
	const entries = $derived(datasetPages(data.view, rowTable));

	const routes = {
		atlas: '/datasets/[id]/atlas',
		graph: '/datasets/[id]/graph',
		tree: '/datasets/[id]/tree',
	} as const satisfies Record<DatasetPage, string>;

	const hrefOf = (target: DatasetPage | null): string =>
		`${resolve(target === null ? '/datasets/[id]' : routes[target], { id: encodeURIComponent(id) })}${kept}`;

	const current = $derived(
		Object.entries(routes).find(([, route]) => route === page.route.id)?.[0] ?? null,
	);

	/** The disabled view whose reason is shown, chosen by the reader. */
	let explained = $state<DatasetPage | null>(null);
	const explanation = $derived(entries.find((e) => e.page === explained)?.unavailable ?? null);

	const tab =
		'focus-visible:ring-ring/50 inline-flex h-8 shrink-0 items-center rounded-md px-2.5 text-sm outline-none focus-visible:ring-3';
</script>

{#if data.failure !== null}
	<div class="flex flex-col gap-2 p-6">
		<h1 class="sr-only">{id}</h1>
		<p class="text-sm" role="alert">
			No dataset <span class="font-mono break-all">{id}</span> that you can see: it does not exist, or
			the lakehouse does not let you read it.
		</p>
		<p class="text-muted-foreground text-xs break-words">{data.failure}</p>
		<a
			class="text-primary text-sm hover:underline"
			href={project === null
				? resolve('/datasets')
				: resolve(`/datasets?${new URLSearchParams({ project })}`)}>All datasets</a
		>
	</div>
{:else if data.view !== null}
	<!-- Each href is a route id resolved by `hrefOf`, with the kept query after it, which the rule cannot see. -->
	<!-- eslint-disable svelte/no-navigation-without-resolve -->
	<nav
		aria-label="Dataset pages"
		class="border-border flex shrink-0 items-center gap-1 overflow-x-auto border-b px-2 py-1"
	>
		<a
			href={hrefOf(null)}
			aria-current={current === null ? 'page' : undefined}
			class={[
				tab,
				'min-w-0 font-mono',
				current === null
					? 'bg-secondary text-foreground'
					: 'text-muted-foreground hover:text-foreground',
			]}><span class="truncate">{id}</span></a
		>
		{#each entries as entry (entry.page)}
			{#if entry.unavailable === null}
				<a
					href={hrefOf(entry.page)}
					aria-current={current === entry.page ? 'page' : undefined}
					class={[
						tab,
						current === entry.page
							? 'bg-secondary text-foreground'
							: 'text-muted-foreground hover:text-foreground',
					]}>{entry.name}</a
				>
			{:else}
				<button
					type="button"
					aria-disabled="true"
					aria-describedby="why-{entry.page}"
					aria-expanded={explained === entry.page}
					class={[tab, 'text-muted-foreground cursor-not-allowed opacity-70']}
					onclick={() => (explained = explained === entry.page ? null : entry.page)}
					>{entry.name}</button
				>
				<span id="why-{entry.page}" hidden>{entry.unavailable}</span>
			{/if}
		{/each}
	</nav>
	{#if explanation}
		<p
			class="text-muted-foreground border-border shrink-0 border-b px-3 py-1.5 text-xs"
			role="status"
		>
			{entries.find((e) => e.page === explained)?.name} is not available: {explanation}.
		</p>
	{/if}
	<!-- Another dataset mounts its pages afresh: no page's state (a map, an open entity, a chosen topic)
	     or late answer outlives the dataset it was read for. -->
	{#key data.id}
		{@render children()}
	{/key}
{/if}
