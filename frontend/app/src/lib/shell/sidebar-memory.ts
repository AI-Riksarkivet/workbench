/**
 * Whether the person keeps the sidebar open, remembered per browser: a convenience of the viewer's own, in
 * `localStorage` under a per-person key like the dock's layout (`#lib/dock/store.ts`'s `personKey`), the
 * anonymous key where nobody signs in. Every access is guarded: where storage is refused, or no key can be
 * made, the sidebar starts collapsed and nothing is remembered.
 */
import { personKey } from '#lib/dock/store.ts';

const PREFIX = 'workbench-sidebar:';

/** The key the person's choice is kept under, or null where none can be made. */
export async function sidebarKey(principal: string | null): Promise<string | null> {
	try {
		return await personKey(PREFIX, principal);
	} catch {
		return null;
	}
}

/** The remembered choice: open, collapsed, or null when nothing is remembered. */
export function rememberedOpen(
	key: string,
	storage: () => Storage = () => localStorage,
): boolean | null {
	try {
		const value = storage().getItem(key);
		return value === 'open' ? true : value === 'collapsed' ? false : null;
	} catch {
		return null;
	}
}

/** Remember the person's choice; a refused write is forgotten with the page. */
export function rememberOpen(
	key: string,
	open: boolean,
	storage: () => Storage = () => localStorage,
): void {
	try {
		storage().setItem(key, open ? 'open' : 'collapsed');
	} catch {
		// Storage refused (a private window, blocked site data, a full quota): the choice lasts the page.
	}
}
