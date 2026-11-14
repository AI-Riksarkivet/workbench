<script lang="ts">
	/** The projects of every organisation the reader belongs to, and the designer that creates one. */
	import { invalidateAll } from '$app/navigation';
	import ProjectsLanding from '#lib/projects/ProjectsLanding.svelte';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import { ANNOTATOR_HINT } from '#lib/projects/presentation.ts';
	import { titleOf } from '#lib/nav.ts';

	let { data } = $props();
	const orgs = $derived(data.me?.orgs.map((o) => o.org) ?? []);
</script>

<svelte:head><title>{titleOf('Projects')}</title></svelte:head>

{#if data.meError !== null}
	<h1 class="sr-only">Projects</h1>
	<LoadFailure
		what="Which organisations you belong to could not be read."
		detail={data.meError}
		hint={ANNOTATOR_HINT}
		onretry={() => void invalidateAll()}
	/>
{:else}
	<ProjectsLanding {orgs} />
{/if}
