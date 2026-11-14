/**
 * The transport for a person's own documents (saved views, dock layouts): this browser's storage, one key per person.
 *
 * The lakehouse keeps no client's state, so the workbench keeps these itself, in the browser. A key carries the
 * SHA-256 of the signed-in person's subject, so a second person on the same browser profile never reads or overwrites
 * the first one's documents. Until the root layout names the person, nothing is read or written.
 *
 * It answers the shapes `$lib/user-state` reads, so the three outcomes stay distinct: nothing stored is
 * `exists: false`, a stored value that does not parse is 409 (unreadable, never overwritten), and no person or storage
 * the browser refuses (a private window, blocked site data) is status 0.
 */
import type { ApiResult } from '#lib/api/client.ts';
import type { UserStateDocument, UserStateEnvelope } from '#lib/explore/user-state.ts';

/** Whose documents this tab reads and writes: the digest of the person's subject, or null while none is known. */
let owner: Promise<string | null> = Promise.resolve(null);

async function digest(subject: string): Promise<string> {
	const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(subject));
	return Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Name the person whose documents this tab keeps; `null` (signed out) keeps none. */
export function setUserStateOwner(subject: string | null): void {
	owner = subject ? digest(subject) : Promise.resolve(null);
}

/** The storage key of one person's document. */
const documentKey = (ownerDigest: string, document: UserStateDocument): string =>
	`workbench-user-state:${ownerDigest}:${document}`;

const NO_PERSON = 'no signed-in person to keep documents for';
const UNAVAILABLE = 'this browser refuses site storage, so nothing can be kept here';

/** Read one document of the signed-in person from this browser's storage. */
export async function readUserStateDoc({
	document,
}: {
	document: UserStateDocument;
}): Promise<ApiResult<UserStateEnvelope>> {
	const who = await owner;
	if (who === null) return { ok: false, status: 0, detail: NO_PERSON };
	let raw: string | null;
	try {
		raw = localStorage.getItem(documentKey(who, document));
	} catch {
		return { ok: false, status: 0, detail: UNAVAILABLE };
	}
	if (raw === null) return { ok: true, data: { exists: false } };
	try {
		return { ok: true, data: { exists: true, value: JSON.parse(raw) as unknown } };
	} catch {
		return { ok: false, status: 409, detail: `the stored ${document} cannot be read` };
	}
}

/** Write one document of the signed-in person to this browser's storage. */
export async function writeUserStateDoc({
	document,
	value,
}: {
	document: UserStateDocument;
	value: unknown;
}): Promise<ApiResult<null>> {
	const who = await owner;
	if (who === null) return { ok: false, status: 0, detail: NO_PERSON };
	try {
		localStorage.setItem(documentKey(who, document), JSON.stringify(value));
	} catch {
		return { ok: false, status: 0, detail: UNAVAILABLE };
	}
	return { ok: true, data: null };
}
