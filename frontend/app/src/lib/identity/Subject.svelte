<script lang="ts">
	// One rendered authorization subject (`type:id`, an OIDC subject). The full subject always rides `title`, so
	// hover and copy still yield the value a grant or a revoke sends back; a component makes that structural, since
	// a call site cannot render the label without it.
	import { subjectDisplay } from './subject.ts';

	let {
		value,
		fallback = 'system',
		class: className = '',
	}: {
		/** The raw subject: an authorization subject, an OIDC subject, or `null` for an unattributed action. */
		value: string | null | undefined;
		/** What to render when there is no subject at all. */
		fallback?: string;
		class?: string;
	} = $props();

	const shown = $derived(subjectDisplay(value ?? fallback));
</script>

<span class="subject mono {className}" title={shown.title}>{shown.label}</span>

<style>
	.subject {
		font-family: ui-monospace, monospace;
	}
</style>
