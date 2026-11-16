<script lang="ts">
	/**
	 * One dataset: its dock (`/datasets/[id]`, named by the dataset's id), whose panels are every view of the
	 * dataset (`#lib/dock`). What the layout adds is the dataset's failure states: a viewer that could not be
	 * reached, told apart from a dataset the reader cannot see.
	 */
	import { page } from '$app/state';
	import { resolve } from '$app/paths';
	import { invalidateAll } from '$app/navigation';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import { datasetView } from '#lib/api/explore.api.ts';
	import { projectOf } from '#lib/project.ts';

	let { data, children } = $props();
	const id = $derived(data.id);
	const project = $derived(projectOf(page.url));
	const isPlain = $derived(data.facts !== null && !data.facts.stamped);
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
	<!-- Another dataset mounts its dock afresh: no panel's state or late answer outlives the dataset it was
	     read for, and the dataset's own layout is restored for it. -->
	{#key data.id}
		{@render children()}
	{/key}
{/if}
