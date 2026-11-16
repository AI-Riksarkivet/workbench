import { error } from '@sveltejs/kit';
import { fetchMe, type Me } from '#lib/api/me.api.ts';
import { installExplorerTransport } from '#lib/api/explorer.ts';
import { setUserStateOwner } from '#lib/explore/browser-user-state.ts';
import { auth, initAuth, login } from '#lib/auth.svelte.ts';
import type { LayoutLoad } from './$types';

// A single-page app: every route renders in the browser, from the static `index.html` fallback.
export const ssr = false;
export const prerender = false;

/**
 * The layout contract every page reads: whether this stack signs people in, and who the reader is to
 * the workbench (the annotator's `GET /me`, the navbar's one source of identity).
 *
 * SIGN-IN FIRST. On a stack that signs people in, a signed-out visit to any page goes to the issuer and
 * comes back to the page it asked for; the `/auth/*` pages are the round trip itself. A `/config.json`
 * the app cannot use is a 503 that says why, and no service is called.
 *
 * `me` is null when the annotator could not answer, and `meError` says why: the navbar then says the
 * identity is unavailable rather than inventing one.
 */
export const load: LayoutLoad = async ({ fetch, url, untrack }) => {
	await initAuth(fetch);
	if (auth.configError) error(503, `Sign-in is not configured: ${auth.configError}`);
	installExplorerTransport();
	// Whose saved searches this tab keeps: the signed-in person; with sign-in off, the one local person.
	setUserStateOwner(auth.enabled ? (auth.session?.sub ?? null) : 'local');
	const path = untrack(() => url.pathname + url.search);
	if (path.startsWith('/auth/')) return { authEnabled: auth.enabled, me: null, meError: null };
	if (auth.enabled && !auth.session) {
		// A load never sees the fragment; the address the browser opened does, and the sign-in round trip
		// returns to all of it.
		await login(`${path}${location.hash}`);
		// A provider that will not sign anyone in to this app's client is said here, not on its own error page.
		if (auth.configError) error(503, `Sign-in is not configured: ${auth.configError}`);
		// The browser is leaving for the issuer; render nothing meanwhile.
		await new Promise<never>(() => {});
	}
	const me = await fetchMe();
	return {
		authEnabled: auth.enabled,
		me: me.ok ? me.data : (null satisfies Me | null),
		meError: me.ok ? null : me.detail,
	};
};
