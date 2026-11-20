/**
 * A session a service refuses, whichever service: the app asks the person ONCE to sign in again, and brings them back
 * to the address they were on.
 *
 * Every answer to a request that carried this tab's credentials is reported here (`setCredentialWatcher`). A 401 from
 * any service's own door, which challenges the credential (`WWW-Authenticate`), means the session it presents is no
 * longer believed (its token expired, or the issuer stopped publishing the key that signed it): the service is added to
 * `refused`, which the shell shows as one prompt however many reads were refused at once, and a later success from
 * the same service takes it away. A 403 is a signed-in person who may not do something, and a 401 a service only relays
 * (the lakehouse refusing the person's read) says nothing about the session: neither asks anything. A session whose
 * expiry passed before it could be renewed asks the same (`sessionEnded`, and the signer's ENDED).
 *
 * The prompt does not leave on its own: a page with work not yet saved keeps it on screen until the person chooses to
 * sign in, and what a page can carry across the round trip it keeps first (`beforeSigningInAgain`, the canvas's unsaved
 * edits). The doors that refused are noted for the return in this tab's sessionStorage (by the URL each request was
 * signed for, without its query); one of them refusing the fresh session as well is said plainly (`failed`) and never
 * sends the person round again. A door that answers the fresh session drops out of the note.
 */
import { login } from './auth.svelte.ts';
import { setCredentialWatcher } from './api/client.ts';
import { serviceOf } from './api/services.ts';

/** Where this tab keeps, across the issuer's round trip, the doors that refused the session it left with. */
const NOTE = 'workbench.reauth-refused';

class Reauth {
	/** The services that refused the session, while the person is asked to sign in again; empty when nothing is. */
	refused = $state.raw<string[]>([]);
	/** A service that refused the session the person just signed in again for: said, never asked again. */
	failed = $state<string | null>(null);
}

export const reauth = new Reauth();

/** The doors refusing the session now, by the service each is of: noted when the person leaves to sign in again. */
const refusedDoors = new Map<string, string>();

/** How long after leaving to sign in again a refusal by a noted door is the fresh session's: a session that ends
 *  hours later asks again as any other does. */
const NOTE_LIFE_MS = 5 * 60_000;

interface Note {
	/** When the person left to sign in again (epoch ms). */
	at: number;
	doors: string[];
}

let remembered: Note = { at: 0, doors: [] };

function noted(): Note {
	let read: unknown = remembered;
	try {
		const raw = sessionStorage.getItem(NOTE);
		read = raw === null ? null : JSON.parse(raw);
	} catch {
		// The page load's own memory.
	}
	if (read === null || typeof read !== 'object' || !('at' in read) || !('doors' in read))
		return { at: 0, doors: [] };
	const at = Number(read.at);
	const doors = Array.isArray(read.doors)
		? read.doors.filter((s): s is string => typeof s === 'string')
		: [];
	return Date.now() - at > NOTE_LIFE_MS ? { at: 0, doors: [] } : { at, doors };
}

function note(next: Note): void {
	remembered = next;
	try {
		if (next.doors.length === 0) sessionStorage.removeItem(NOTE);
		else sessionStorage.setItem(NOTE, JSON.stringify(next));
	} catch {
		// Kept in `remembered` alone.
	}
}

/** Whether the session ended before any service said so (its expiry passed and it could not be renewed). */
let ended = false;

/** The prompt names what refused: each service with a door still refusing, and the workbench for an ended session. */
function show(): void {
	const services = [...new Set([...refusedDoors.values(), ...(ended ? ['workbench'] : [])])];
	if (services.join() !== reauth.refused.join()) reauth.refused = services;
}

/** What a request that carried the session, signed for ``htu``, was answered, and whether it challenged it. Only a
 *  challenged 401 is the session refused: a 401 a service relays from elsewhere (a lakehouse refusing the person's
 *  read) carries no challenge and asks nothing. A success from a service clears what it said. */
function answered(status: number, htu: string, challenged: boolean): void {
	const service = serviceOf(htu) ?? 'workbench';
	const held = noted();
	if (status === 401 && challenged) {
		if (held.doors.includes(htu)) {
			reauth.failed ??= service;
			return;
		}
		refusedDoors.set(htu, service);
		show();
		return;
	}
	if (status < 200 || status >= 300) return;
	if (held.doors.includes(htu)) note({ at: held.at, doors: held.doors.filter((d) => d !== htu) });
	// A service verifies a session one way for all its doors, so one it accepted again is no longer refused there.
	for (const [door, of] of refusedDoors) if (of === service) refusedDoors.delete(door);
	// A credential a service accepted is a session that has not ended.
	ended = false;
	show();
}

setCredentialWatcher(answered);

/** The session ended and could not be renewed, before any service said so: the same one prompt. */
export function sessionEnded(): void {
	ended = true;
	show();
}

const hooks = new Set<() => void>();

/** Run ``keep`` just before the browser leaves to sign in again, so what it keeps survives the round trip; the
 *  returned function stops it. */
export function beforeSigningInAgain(keep: () => void): () => void {
	hooks.add(keep);
	return () => hooks.delete(keep);
}

/** Leave for the issuer and come back to ``returnTo``, keeping first what each page asked to keep. */
export function signInAgain(returnTo: string): void {
	for (const keep of hooks) keep();
	note({ at: Date.now(), doors: [...new Set([...noted().doors, ...refusedDoors.keys()])] });
	void login(returnTo);
}
