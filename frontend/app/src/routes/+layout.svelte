<script lang="ts">
	import '../app.css';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { appearance } from '@riksarkivet/ui/appearance';
	import { APPEARANCE } from '#lib/appearance-options.ts';
	import type { Snippet } from 'svelte';
	import { AppShell, type NavUser } from '#lib/shell/index.ts';
	import SessionPrompt from '#lib/shell/session-prompt.svelte';
	import { auth, RENEW_MARGIN_SECONDS, renewSession } from '#lib/auth.svelte.ts';
	import { sessionEnded } from '#lib/reauth.svelte.ts';
	import { activity } from '#lib/activity.svelte.ts';
	import { track } from '#lib/view-transition.svelte.ts';
	import { appSidebar, PAGE_NAMES } from '#lib/nav.ts';
	import { linkWith, projectOf } from '#lib/project.ts';
	import type { LayoutData } from './$types';

	let { children, data }: { children: Snippet; data: LayoutData } = $props();

	// Every in-app link carries the reader's project (`?project=`), so it survives navigation; on a project's own page
	// the project is the one its path names.
	const link = $derived(linkWith(page.params.project ?? projectOf(page.url)));
	const pageNav = $derived(appSidebar(link));

	/** Up-to-two-letter initials for the avatar. */
	function initialsOf(name: string): string {
		const words = name.trim().split(/\s+/).filter(Boolean);
		const first = words[0] ?? name;
		const second = words[1];
		return (second ? `${first[0] ?? ''}${second[0] ?? ''}` : first.slice(0, 2)).toUpperCase();
	}

	// The identity comes from the workbench's own `GET /me`. When it could not be read, the reader is
	// still signed in: the navbar says the identity is unavailable, and sign-out stays reachable.
	const user = $derived.by((): NavUser | null => {
		const me = data.me;
		if (me) {
			const name = me.name ?? me.email ?? me.sub;
			return { name, initials: initialsOf(name), ...(me.email ? { email: me.email } : {}) };
		}
		if (data.meError !== null && auth.session) {
			return { name: 'Identity unavailable', email: data.meError, initials: '!' };
		}
		return null;
	});

	// The session ends when its token does: it is renewed in place where the provider gave a refresh token, and
	// otherwise the person is asked to sign in again (the shell's one prompt), never sent away with their work on screen.
	async function renewOrAsk(): Promise<void> {
		if (!(await renewSession())) sessionEnded();
	}
	$effect(() => {
		const session = auth.session;
		// Tokens dropped (a sign-out here or in another tab): nothing left to renew, and the timer stops.
		if (!session || session.expiresAt === 0 || !auth.tokensHeld) return;
		const wait = Math.max((session.expiresAt - RENEW_MARGIN_SECONDS) * 1000 - Date.now(), 0);
		const timer = setTimeout(() => void renewOrAsk(), wait);
		return () => clearTimeout(timer);
	});
	// A tab the browser held in the background may come back past its expiry before the timer runs (timers there are
	// clamped): it asks at once, on its return.
	function returned(): void {
		const session = auth.session;
		if (
			document.visibilityState !== 'visible' ||
			!session ||
			session.expiresAt === 0 ||
			!auth.tokensHeld
		)
			return;
		if (session.expiresAt - RENEW_MARGIN_SECONDS <= Date.now() / 1000) void renewOrAsk();
	}

	// The bell: the outcome of every write this app sent for the reader, kept in their browser.
	const activityFeed = $derived({
		entries: activity.entries,
		unread: activity.unread,
		onopen: () => activity.markRead(),
		onclear: () => activity.clear(),
	});

	// The person's theme, light or dark, and accent: kept in this browser and already on the page before it painted
	// (`/appearance-boot.js`, which the design system's Vite plugin builds with the same options); from here the store
	// owns it, and follows the system when the mode is `system`.
	$effect(() => appearance.start(APPEARANCE));

	onNavigate((navigation) => {
		if (!document.startViewTransition) return;
		// A change of the address alone (a page writing its view into it, `#lib/dock/link.ts`) stays on the
		// page it is on: it gets no page transition.
		if (navigation.from?.url.pathname === navigation.to?.url.pathname) return;
		return new Promise((done) => {
			track(
				document.startViewTransition(async () => {
					done();
					await navigation.complete;
				}),
			);
		});
	});
</script>

<svelte:document onvisibilitychange={returned} />

<!-- The sign-in round trip is no page of the app: it shows no sidebar or breadcrumb, only its own line. -->
{#if page.route.id?.startsWith('/auth/')}
	{@render children()}
{:else}
	<AppShell
		pathname={page.url.pathname}
		routeId={page.route.id}
		{user}
		authEnabled={data.authEnabled}
		principal={auth.enabled ? (auth.session?.sub ?? null) : null}
		{pageNav}
		pages={PAGE_NAMES}
		activity={activityFeed}
		{link}
	>
		<SessionPrompt />
		{@render children()}
	</AppShell>
{/if}
