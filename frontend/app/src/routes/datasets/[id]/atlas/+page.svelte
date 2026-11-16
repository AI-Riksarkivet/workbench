<script lang="ts">
	/**
	 * A saved link to the dataset's atlas as a page of its own lands on the dock with the panel in front. Done here,
	 * not in a load: a load never sees the link's fragment, which the landing keeps.
	 */
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { panelHref } from '../to-panel.ts';

	onMount(() => {
		// The route id is resolved inside `panelHref`; the query the rule cannot see follows it.
		// oxlint-disable-next-line svelte/no-navigation-without-resolve
		goto(panelHref(page.params.id ?? '', 'atlas', page.url), { replace: true }).catch(() => {
			// Overtaken by another navigation, which lands where it was going instead.
		});
	});
</script>
