<script lang="ts">
	// `/auth/logout` — the shell's sign-out link: forget the reader's activity log and any canvas edits kept across a
	// sign-in, drop the in-memory session, end the issuer's.
	import { onMount } from 'svelte';
	import { activity } from '#lib/activity.svelte.ts';
	import { logout } from '#lib/auth.svelte.ts';
	import { forgetPendingEdits } from '#lib/canvas/pending.ts';

	onMount(() => {
		activity.forget();
		forgetPendingEdits();
		void logout();
	});
</script>

<svelte:head><title>Signing out · workbench</title></svelte:head>

<main class="text-muted-foreground p-8 text-sm">Signing out…</main>
