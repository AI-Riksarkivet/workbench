<script lang="ts">
	/**
	 * A read that failed, said in the reader's terms: what could not be loaded, what to do about it, and
	 * the service's own words beneath for whoever has to look into it, with a way to try again.
	 */
	import { Button } from '@riksarkivet/ui/button';

	let {
		what,
		detail,
		hint = 'Try again. If it keeps failing, the dataset may be unavailable right now.',
		onretry,
	}: {
		/** What could not be loaded, as a sentence: "The embedding map could not be loaded." */
		what: string;
		/** The service's own description of the failure. */
		detail: string;
		/** What to do about it, in the reader's terms. */
		hint?: string;
		onretry: () => void;
	} = $props();
</script>

<div class="flex max-w-md flex-col items-center gap-2 p-6 text-center">
	<p class="text-destructive text-sm font-medium" role="alert">{what}</p>
	<p class="text-muted-foreground text-xs">{hint}</p>
	<p class="text-muted-foreground text-xs break-words">Details: {detail}</p>
	<Button variant="outline" size="sm" onclick={onretry}>Try again</Button>
</div>
