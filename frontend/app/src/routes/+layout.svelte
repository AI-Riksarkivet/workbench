<script lang="ts">
	import '../app.css';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import type { Snippet } from 'svelte';
	import { AppShell, type NavUser } from '#lib/shell/index.ts';
	import { auth, expired, login } from '#lib/auth.svelte.ts';
	import { activity } from '#lib/activity.svelte.ts';
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

	// The session ends when its token does: there is no refresh token, so the browser goes back to the
	// issuer and returns here.
	$effect(() => {
		const session = auth.session;
		if (!session || session.expiresAt === 0) return;
		const wait = Math.max(session.expiresAt * 1000 - Date.now(), 0);
		const timer = setTimeout(() => {
			if (expired(session)) void login(page.url.pathname + page.url.search);
		}, wait);
		return () => clearTimeout(timer);
	});

	// The bell: the outcome of every write this app sent for the reader, kept in their browser.
	const activityFeed = $derived({
		entries: activity.entries,
		unread: activity.unread,
		onopen: () => activity.markRead(),
		onclear: () => activity.clear(),
	});

	onNavigate((navigation) => {
		if (!document.startViewTransition) return;
		return new Promise((done) => {
			document.startViewTransition(async () => {
				done();
				await navigation.complete;
			});
		});
	});
</script>

<ModeWatcher defaultMode="dark" />

<!-- The sign-in round trip is no page of the app: it shows no sidebar or breadcrumb, only its own line. -->
{#if page.route.id?.startsWith('/auth/')}
	{@render children()}
{:else}
	<AppShell
		pathname={page.url.pathname}
		routeId={page.route.id}
		{user}
		authEnabled={data.authEnabled}
		{pageNav}
		pages={PAGE_NAMES}
		activity={activityFeed}
		{link}
	>
		{@render children()}
	</AppShell>
{/if}
