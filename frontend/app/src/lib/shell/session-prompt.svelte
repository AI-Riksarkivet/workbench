<script lang="ts">
	// THE ONE PROMPT TO SIGN IN AGAIN, over every page: shown when any service refuses the session (`#lib/reauth.svelte.ts`),
	// however many reads were refused at once, and never on its own leaving the page, so work not yet saved stays on screen
	// until the person chooses. A service that refuses the session they just signed in again for is said, not asked again.
	import { page } from '$app/state';
	import { Button } from '@riksarkivet/ui/button';
	import { LogIn } from '@lucide/svelte';
	import { reauth, signInAgain } from '#lib/reauth.svelte.ts';

	const which = (services: readonly string[]): string =>
		services.map((s) => `the ${s}`).join(services.length === 2 ? ' and ' : ', ');
</script>

{#if reauth.failed !== null}
	<div
		role="alert"
		class="border-destructive bg-destructive/10 text-foreground flex flex-wrap items-center gap-2 border-b px-4 py-2 text-sm"
		data-testid="session-failed"
	>
		You signed in again, and {which([reauth.failed])} still refuses your sign-in. Reload the page to try
		again; if it keeps happening, the workbench's sign-in needs its administrator.
	</div>
{:else if reauth.refused.length > 0}
	<div
		role="alert"
		class="border-warning bg-warning/10 text-foreground flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-2 text-sm"
		data-testid="session-prompt"
	>
		<span class="min-w-0 flex-1">
			Your sign-in has ended or was refused by {which(reauth.refused)}, so what this page shows may
			be incomplete and nothing more is saved. Sign in again to go on: you come back to this page,
			with the canvas's unsaved edits.
		</span>
		<Button
			size="sm"
			data-testid="session-sign-in"
			onclick={() => signInAgain(page.url.pathname + page.url.search + page.url.hash)}
		>
			<LogIn class="size-3.5" /> Sign in again
		</Button>
	</div>
{/if}
