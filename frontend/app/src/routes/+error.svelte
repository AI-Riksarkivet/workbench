<script lang="ts">
	import { page } from '$app/state';
	import { resolve } from '$app/paths';
	import { AppError } from '#lib/shell/index.ts';
	import { titleOf } from '#lib/nav.ts';

	/** What a status means to a reader, when the refusal named nothing more specific. */
	function headingFor(status: number): string {
		if (status === 403) return 'You don’t have access to this page';
		if (status === 404) return 'Page not found';
		if (status === 503) return 'The workbench is unavailable';
		return 'Something went wrong';
	}

	const heading = $derived(page.error?.title ?? headingFor(page.status));
</script>

<svelte:head><title>{titleOf(heading)}</title></svelte:head>

<AppError
	status={page.status}
	{heading}
	message={page.error?.message}
	home={resolve('/datasets')}
/>
