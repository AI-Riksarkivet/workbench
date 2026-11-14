/**
 * A person's own documents (saved views, dock layouts): read and written through an injected transport, the browser
 * store `$lib/browser-user-state` in the app and a stub in tests, so this module is a pure function of what the store
 * answered.
 *
 * Three outcomes, and keeping them distinct is the whole point:
 *
 *   - `ok`        a document came back. Use it.
 *   - `absent`    the person has never saved one. Seed a fresh one; saving is safe.
 *   - `unreadable` a document EXISTS and could not be read, or the store cannot be used. Do NOT seed and do NOT save:
 *                 treated as empty, the next autosave would overwrite a record that is still there.
 */
import type { ApiResult } from '@workbench/api/client';

/** The documents a person keeps. */
export type UserStateDocument = 'workflow-graph' | 'saved-views';

/** What a store answers for a document: `exists: false` is a genuine "never saved", and a value is only a value when
 *  both keys agree. */
export interface UserStateEnvelope {
	exists?: boolean;
	value?: unknown;
}

export type UserStateRead<T> =
	| { readonly status: 'ok'; readonly value: T }
	| { readonly status: 'absent' }
	| { readonly status: 'unreadable'; readonly detail: string };

/** The read half of the transport: the caller's document, or the status the store answered with. */
export type UserStateReader = (args: {
	document: UserStateDocument;
}) => Promise<ApiResult<UserStateEnvelope>>;

/** The write half. Only `ok` counts as a save. */
export type UserStateWriter = (args: {
	document: UserStateDocument;
	value: unknown;
}) => Promise<ApiResult<unknown>>;

/**
 * Read the caller's document.
 *
 * @param read the transport: `readUserStateDoc` in the app, a stub in tests.
 */
export async function readUserState<T>(
	document: UserStateDocument,
	read: UserStateReader,
): Promise<UserStateRead<T>> {
	// A transport that rejects rather than answering is a store nobody could ask: unreadable, never absent.
	let result: ApiResult<UserStateEnvelope>;
	try {
		result = await read({ document });
	} catch (e) {
		result = {
			ok: false,
			status: 0,
			detail: e instanceof Error ? e.message : 'the store is unreachable',
		};
	}

	if (result.ok) {
		const body = result.data;
		if (body.exists !== true || body.value === undefined) return { status: 'absent' };
		return { status: 'ok', value: body.value as T };
	}
	if (result.status === 0) {
		return { status: 'unreadable', detail: result.detail || 'the store is unreachable' };
	}
	if (result.status === 409) {
		return { status: 'unreadable', detail: result.detail || 'the stored document cannot be read' };
	}
	return { status: 'unreadable', detail: `the store answered ${result.status}` };
}

/**
 * Save the caller's document. Returns whether the store took it.
 *
 * Callers must not call this after a read returned `unreadable`: that is the overwrite this module exists to prevent.
 *
 * @param write the transport: `writeUserStateDoc` in the app, a stub in tests.
 */
export async function writeUserState(
	document: UserStateDocument,
	value: unknown,
	write: UserStateWriter,
): Promise<boolean> {
	try {
		return (await write({ document, value })).ok;
	} catch {
		return false;
	}
}
