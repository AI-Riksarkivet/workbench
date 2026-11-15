/**
 * Signing in again after a 401, once per page: a read refused because the session ended sends the person to the
 * issuer and back, and a page that is refused again right after is not sent round a second time (that would loop
 * between the page and the issuer). The marker is kept in this tab's sessionStorage under the page it was made for, and
 * dropped once a read there succeeds.
 */
import { login } from './auth.svelte.ts';

const PREFIX = 'workbench.signin-tried:';
/** Where sessionStorage is not available, a page load's own memory. */
const tried = new Set<string>();

/** Sign in again and come back to `returnTo`, unless that was already tried from here: then false, and the page says it
 *  could not sign the person in. */
export function signInAgainOnce(returnTo: string): boolean {
	try {
		if (sessionStorage.getItem(PREFIX + returnTo) !== null) return false;
		sessionStorage.setItem(PREFIX + returnTo, '1');
	} catch {
		if (tried.has(returnTo)) return false;
		tried.add(returnTo);
	}
	void login(returnTo);
	return true;
}

/** A read at `returnTo` succeeded: a later 401 there may send the person round again. */
export function signedInAt(returnTo: string): void {
	tried.delete(returnTo);
	try {
		sessionStorage.removeItem(PREFIX + returnTo);
	} catch {
		// Nothing was kept.
	}
}
