<script lang="ts">
	/**
	 * Every page of one dataset: its dock (`/datasets/[id]`, named by the dataset's id) and its views, the
	 * atlas, the graph and the tree (`#lib/dataset-nav.ts`), reached from one row of links. A view the
	 * dataset cannot show stays in the row, disabled, and says why in a tooltip anchored to it while it is focused or
	 * hovered, and while chosen (a tap), so a keyboard, a screen reader and a touch reach the reason as a pointer does,
	 * and nothing below the row moves.
	 */
	import { page } from '$app/state';
	import { resolve } from '$app/paths';
	import { invalidateAll } from '$app/navigation';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import { datasetView } from '#lib/api/explore.api.ts';
	import { DATASET_PAGE_NAMES, datasetPages, type DatasetPage } from '#lib/dataset-nav.ts';
	import { projectOf } from '#lib/project.ts';
	import { datasetLabel } from '#lib/dataset-label.ts';
	import * as Tooltip from '@workbench/ui/tooltip';

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
	const isPlain = $derived(data.facts !== null && !data.facts.stamped);
	const entries = $derived(datasetPages(data.view, rowTable, isPlain));

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

	/** The disabled view whose reason is shown: the one focused or hovered, else the one chosen. */
	let explained = $state<DatasetPage | null>(null);
	let peeked = $state<DatasetPage | null>(null);
	const shown = $derived(peeked ?? explained);

	const tab =
		'focus-visible:ring-ring/50 inline-flex h-8 shrink-0 items-center rounded-md px-2.5 text-sm outline-none focus-visible:ring-3';
</script>

{#if data.failure !== null && data.unreachable}
	<div class="flex flex-col items-start p-6">
		<h1 class="sr-only">{id}</h1>
		<LoadFailure
			what="The viewer could not be reached, so this dataset cannot be shown right now."
			hint="Try again. If it keeps failing, the viewer may be down."
			detail={data.failure}
			onretry={() =>
				void datasetView(id)
					.refresh()
					.finally(() => invalidateAll())}
		/>
	</div>
{:else if data.failure !== null}
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
{:else if data.view !== null || isPlain}
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
			]}><span class="truncate">{datasetLabel(id)}</span></a
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
				<!-- Controlled: the tooltip shows while the tab is focused or hovered, and stays for a tab that was
				     chosen, which is how a touch, which neither hovers nor focuses, reaches the reason. -->
				<Tooltip.Root
					open={shown === entry.page}
					onOpenChange={(open) => {
						if (open) peeked = entry.page;
						else if (peeked === entry.page) peeked = null;
					}}
				>
					<Tooltip.Trigger>
						{#snippet child({ props })}
							<button
								{...props}
								type="button"
								aria-disabled="true"
								aria-describedby="why-{entry.page}"
								aria-expanded={explained === entry.page}
								class={[tab, 'text-muted-foreground cursor-not-allowed opacity-70']}
								onclick={() => (explained = explained === entry.page ? null : entry.page)}
								onfocus={() => (peeked = entry.page)}
								onmouseenter={() => (peeked = entry.page)}
								onmouseleave={() => {
									if (peeked === entry.page) peeked = null;
								}}
								onblur={() => {
									if (peeked === entry.page) peeked = null;
									if (explained === entry.page) explained = null;
								}}>{entry.name}</button
							>
						{/snippet}
					</Tooltip.Trigger>
					<Tooltip.Content side="bottom" role="tooltip">
						{entry.name} is not available: {entry.unavailable}.
					</Tooltip.Content>
				</Tooltip.Root>
				<span id="why-{entry.page}" class="sr-only">{entry.unavailable}</span>
			{/if}
		{/each}
	</nav>
	<!-- Another dataset mounts its pages afresh: no page's state (a map, an open entity, a chosen topic)
	     or late answer outlives the dataset it was read for. -->
	{#key data.id}
		{#if isPlain && current !== null}
			<p class="text-muted-foreground p-6 text-sm" role="status">
				{DATASET_PAGE_NAMES[current as DatasetPage]} is not available: this dataset carries no workbench
				descriptor, so it is shown as a plain table.
			</p>
		{:else}
			{@render children()}
		{/if}
	{/key}
{/if}
