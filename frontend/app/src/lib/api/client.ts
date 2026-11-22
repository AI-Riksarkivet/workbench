// #lib/api/client — the browser's one transport to the workbench's services.
//
// The app is a single-page app with no backend of its own: every call leaves the browser for this
// origin's `/api/*`, which the platform's Gateway routes to the service that owns the prefix
// (`/config.json`'s `services`). Each service verifies the caller on every request, so nothing in between
// holds a credential.
//
// WHERE THE PROVIDER BINDS THE TOKEN TO THIS TAB'S KEY (DPoP, RFC 9449), every request carries
// `Authorization: DPoP <token>` and a `DPoP` proof signed for exactly that request: its method (`htm`) and
// its URL without query or fragment (`htu`, § 4.2; the services compare it so,
// `workbench.governed.dpop`). One proof per request, never reused: each carries a fresh `jti`, which a
// service remembers against a replay. Where it binds none (`auth.svelte.ts` reads which from its
// discovery), the token goes as `Authorization: Bearer <token>`, alone.
// oidc-client-ts signs `htu` exactly as it is given (3.5.0's `generateDPoPProof` puts the url argument in
// the claim unchanged), so the query and fragment are stripped HERE, before the signer sees the URL.
//
// The proof comes from a SIGNER the app installs once (`setSigner`, from `auth.svelte.ts`), so this
// module holds no token and no key, and stays testable without a browser.
import * as v from 'valibot';
import type { WriteKey } from '../write-key.svelte.ts';

/** Per-request timeout for a read: a hung service must not stack reads. */
const FETCH_TIMEOUT_MS = 8000;

/** A read that carries a file (an image to search by) uploads it and runs its encoder
 *  first, which the first call after a service restart loads: given longer than a plain read. */
const UPLOAD_TIMEOUT_MS = 30_000;

/** A write's timeout: a write given up on may land anyway, so it is waited for, and a page tells the
 *  person only that it is NOT CONFIRMED when no answer came. */
const WRITE_TIMEOUT_MS = 120_000;

/** What a request is sent with: the token, and the proof signed for it when the token is bound to a key. */
export interface Credentials {
	token: string;
	proof: string | null;
}

/** A session that has ended and could not be renewed: the request goes without credentials, and is reported to the
 *  watcher as the session refused, whatever the service answers. */
export const ENDED = 'ended' as const;

/** Signs one request: `htu` is already stripped of query and fragment. Null when nobody is signed in
 *  (or the stack signs nobody in): the request then goes without credentials. */
export type Signer = (htu: string, method: string) => Promise<Credentials | typeof ENDED | null>;

let signer: Signer = async () => null;

/** Install the source of credentials (the app's in-memory OIDC session, and its DPoP key where it has one). */
export function setSigner(next: Signer): void {
	signer = next;
}

/** Told the status of every answer to a request that carried this tab's credentials, the `htu` it was signed for, and
 *  whether the answer challenged the credential (`WWW-Authenticate`, RFC 6750 § 3, RFC 9449 § 7.1): how the app learns,
 *  whichever service answered, that the session it presents is refused (`#lib/reauth.svelte.ts`). A service's own door
 *  challenges what it refuses; a 401 it only relays (a lakehouse refusing the person's read) carries no challenge and
 *  says nothing about the session. A request sent with no credentials is never reported. */
export type CredentialWatcher = (status: number, htu: string, challenged: boolean) => void;

let watcher: CredentialWatcher = () => {};

export function setCredentialWatcher(next: CredentialWatcher): void {
	watcher = next;
}

function origin(): string | null {
	return typeof location === 'undefined' ? null : location.origin;
}

/** The `htu` a request to `url` is signed for: absolute on this origin, with no query or fragment
 *  (RFC 9449 § 4.2). Null for a URL on another origin, which is never handed a credential. */
export function htuOf(url: string, here: string | null = origin()): string | null {
	if (here === null) return null;
	let parsed: URL;
	try {
		parsed = new URL(url, here);
	} catch {
		return null;
	}
	if (parsed.origin !== new URL(here).origin) return null;
	parsed.search = '';
	parsed.hash = '';
	return parsed.href;
}

/** `fetch` with this tab's credentials, a DPoP proof signed for this one request where the token is bound, and only
 *  to this origin. */
export async function authorizedFetch(input: string, init: RequestInit = {}): Promise<Response> {
	const headers = new Headers(init.headers);
	const method = (init.method ?? 'GET').toUpperCase();
	const htu = htuOf(input);
	let signed = false;
	let ended = false;
	if (htu !== null && !headers.has('authorization')) {
		const credentials = await signer(htu, method);
		if (credentials === ENDED) ended = true;
		else if (credentials?.proof) {
			headers.set('authorization', `DPoP ${credentials.token}`);
			headers.set('dpop', credentials.proof);
		} else if (credentials) {
			headers.set('authorization', `Bearer ${credentials.token}`);
		}
		signed = credentials !== null && !ended;
	}
	// A session that ended is reported before the request goes, so the person is asked even if the answer never comes.
	if (ended && htu !== null) watcher(401, htu, true);
	const res = await fetch(input, { ...init, method, headers });
	if (signed && htu !== null) watcher(res.status, htu, res.headers.has('www-authenticate'));
	return res;
}

// AbortSignal.timeout needs Safari 16+/Chrome 103+; vite transpiles syntax, not APIs.
function timeoutSignal(ms: number = FETCH_TIMEOUT_MS): AbortSignal {
	if (typeof AbortSignal.timeout === 'function') return AbortSignal.timeout(ms);
	const controller = new AbortController();
	setTimeout(() => controller.abort(), ms);
	return controller.signal;
}

/** How many times a read is sent again after a 503 that says when to come back, and how long it waits for that in all.
 *  A service answers so while it cannot yet judge the session's token (`workbench.governed.oidc`: its issuer's key
 *  set is being read, or is read again within its pace, at most 30 s away), never to say the session is refused; so a
 *  read right after the person signed in again at a restarted issuer finds the new key by itself rather than failing.
 *  A 503 without `Retry-After`, or one asking for longer than is left, is the read's answer. Reads alone: a write goes
 *  once (`writeJSON`). */
const READ_RETRIES = 3;
const READ_RETRY_BUDGET_MS = 30_000;

/** The wait a 503 asks for (`Retry-After` in seconds, RFC 9110 § 10.2.3), or null for any other answer. */
function retryAfterMs(res: Response): number | null {
	if (res.status !== 503) return null;
	const raw = res.headers.get('retry-after')?.trim() ?? '';
	return /^\d+$/.test(raw) ? Number(raw) * 1000 : null;
}

function pause(ms: number, signal: AbortSignal | null | undefined): Promise<void> {
	return new Promise((resolve, reject) => {
		if (signal?.aborted) {
			reject(signal.reason);
			return;
		}
		const onAbort = (): void => {
			clearTimeout(timer);
			reject(signal?.reason);
		};
		const timer = setTimeout(() => {
			signal?.removeEventListener('abort', onAbort);
			resolve();
		}, ms);
		signal?.addEventListener('abort', onAbort, { once: true });
	});
}

/** A read through {@link authorizedFetch}, each send given its own `signal()`, sent again as {@link READ_RETRIES} says. */
async function readFetch(
	input: string,
	init: RequestInit,
	signal: () => AbortSignal,
): Promise<Response> {
	let waited = 0;
	for (let attempt = 0; ; attempt++) {
		const res = await authorizedFetch(input, { ...init, signal: signal() });
		const wait = retryAfterMs(res);
		if (wait === null || attempt >= READ_RETRIES || waited + wait > READ_RETRY_BUDGET_MS)
			return res;
		await res.body?.cancel().catch(() => {});
		await pause(wait, init.signal);
		waited += wait;
	}
}

/** {@link authorizedFetch} given up after a read's timeout: the fetch the explorer's client calls
 *  through (`@workbench/explorer-api`'s transport), so none of its reads can hang a panel. A request
 *  with a body is a read that uploads, given {@link UPLOAD_TIMEOUT_MS}. */
export function timedFetch(input: string, init: RequestInit = {}): Promise<Response> {
	const uploads = init.body !== undefined && init.body !== null;
	return readFetch(
		input,
		init,
		() => init.signal ?? timeoutSignal(uploads ? UPLOAD_TIMEOUT_MS : FETCH_TIMEOUT_MS),
	);
}

/** A fetch outcome that keeps the HTTP status — `status: 0` is UNREACHABLE (no answer came), `n` is an
 *  answer with the service's `detail`, and `502` is contract drift (a body that fails its schema). `sessionRefused` is
 *  a 401 that challenged the credential (`WWW-Authenticate`): the session refused, which the shell's one prompt answers
 *  (`#lib/reauth.svelte.ts`), never a 401 a service only relays. */
export type ApiResult<T> =
	| { ok: true; data: T }
	| { ok: false; status: number; detail: string; type?: string; sessionRefused?: true };

/** Lift `detail` out of an error body (FastAPI's `{detail}` and problem+json's `detail` alike). */
function detailFrom(body: unknown, fallback: string): string {
	if (body !== null && typeof body === 'object' && 'detail' in body)
		return typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
	return fallback;
}

/** The problem's own name (RFC 9457 `type`, the fragment of an `about:blank#<name>`), which says what failed where the
 *  detail says it in the words of one item. */
function problemType(body: unknown): string | undefined {
	if (body === null || typeof body !== 'object' || !('type' in body)) return undefined;
	return typeof body.type === 'string' ? body.type.slice(body.type.indexOf('#') + 1) : undefined;
}

async function failure(res: Response, upstream: string): Promise<ApiResult<never>> {
	const body: unknown = await res.json().catch(() => ({}));
	const detail = detailFrom(body, `${upstream} answered ${res.status}`);
	const type = problemType(body);
	const refused = { ok: false as const, status: res.status, detail, ...(type ? { type } : {}) };
	return res.status === 401 && res.headers.has('www-authenticate')
		? { ...refused, sessionRefused: true }
		: refused;
}

/** A read of JSON, given up after {@link FETCH_TIMEOUT_MS}, parsed against `schema`. Never throws. */
export async function readJSON<T>(
	url: string,
	schema: v.GenericSchema<unknown, T>,
	upstream: string,
	init: RequestInit = {},
	onResponse?: (res: Response) => void,
): Promise<ApiResult<T>> {
	let res: Response;
	try {
		res = await readFetch(url, init, () => timeoutSignal());
	} catch (err) {
		return { ok: false, status: 0, detail: `${upstream} is unreachable (${String(err)})` };
	}
	if (!res.ok) return failure(res, upstream);
	onResponse?.(res);
	let body: unknown;
	try {
		body = await res.json();
	} catch (err) {
		return {
			ok: false,
			status: 502,
			detail: `${upstream} answered a non-JSON body: ${String(err)}`,
		};
	}
	const read = v.safeParse(schema, body);
	return read.success
		? { ok: true, data: read.output }
		: { ok: false, status: 502, detail: `${upstream} contract drift: ${v.summarize(read.issues)}` };
}

/** {@link readJSON} with the answer's `Date` (RFC 9110 § 6.6.1): the server's clock when it answered,
 *  which an absolute time in the body is read against. */
export async function readJSONDated<T>(
	url: string,
	schema: v.GenericSchema<unknown, T>,
	upstream: string,
	init: RequestInit = {},
): Promise<ApiResult<{ body: T; date: string | null }>> {
	let date: string | null = null;
	const result = await readJSON(url, schema, upstream, init, (res) => {
		date = res.headers.get('date');
	});
	return result.ok ? { ok: true, data: { body: result.data, date } } : result;
}

/** The binary sibling of {@link readJSON}: the response's bytes (an Arrow IPC stream), its ETag, and its
 *  headers for a door that answers more beside the body (the annotations' version). A `signal` in `init` gives the read
 *  up beside its timeout. */
export async function readBytes(
	url: string,
	upstream: string,
	init: RequestInit = {},
): Promise<ApiResult<{ bytes: Uint8Array; etag: string | null; headers: Headers }>> {
	let res: Response;
	const given = init.signal;
	try {
		res = await readFetch(url, init, () =>
			given ? AbortSignal.any([timeoutSignal(), given]) : timeoutSignal(),
		);
	} catch (err) {
		return { ok: false, status: 0, detail: `${upstream} is unreachable (${String(err)})` };
	}
	if (!res.ok) return failure(res, upstream);
	return {
		ok: true,
		data: {
			bytes: new Uint8Array(await res.arrayBuffer()),
			etag: res.headers.get('etag'),
			headers: res.headers,
		},
	};
}

/** How a write goes: its method (POST unless named), the key that makes a resend converge, and a signal that gives it up
 *  (the person stopped it, or left the page). */
interface WriteOptions {
	method?: string;
	key?: WriteKey;
	signal?: AbortSignal;
}

/** A write of JSON, waited for up to {@link WRITE_TIMEOUT_MS}. `key` makes a resend converge: the write
 *  goes with `Idempotency-Key`, the same one while an unsettled write of the same body is retried
 *  (`WriteKey`), so the service applies it once. A write with no answer is `status: 0`, whose detail says
 *  it is NOT CONFIRMED: it may have landed. */
export async function writeJSON<T>(
	url: string,
	body: unknown,
	schema: v.GenericSchema<unknown, T>,
	upstream: string,
	options: WriteOptions = {},
): Promise<ApiResult<T>> {
	const text = JSON.stringify(body);
	const result = await sendWrite({ url, text, upstream }, schema, {
		method: options.method ?? 'POST',
		idempotencyKey: options.key?.take(text),
		signal: options.signal,
	});
	options.key?.settle(result);
	return result;
}

async function sendWrite<T>(
	{ url, text, upstream }: { url: string; text: string; upstream: string },
	schema: v.GenericSchema<unknown, T>,
	{
		method,
		idempotencyKey,
		signal,
	}: { method: string; idempotencyKey: string | undefined; signal: AbortSignal | undefined },
): Promise<ApiResult<T>> {
	let res: Response;
	try {
		res = await authorizedFetch(url, {
			method,
			headers: {
				'content-type': 'application/json',
				...(idempotencyKey ? { 'idempotency-key': idempotencyKey } : {}),
			},
			body: text,
			signal: signal
				? AbortSignal.any([timeoutSignal(WRITE_TIMEOUT_MS), signal])
				: timeoutSignal(WRITE_TIMEOUT_MS),
		});
	} catch (err) {
		return {
			ok: false,
			status: 0,
			detail: `NOT CONFIRMED: ${upstream} gave no answer (${String(err)}), so the write may have landed`,
		};
	}
	if (!res.ok) return failure(res, upstream);
	let payload: unknown;
	try {
		payload = await res.json();
	} catch (err) {
		return {
			ok: false,
			status: 502,
			detail: `${upstream} answered a non-JSON body: ${String(err)}`,
		};
	}
	const read = v.safeParse(schema, payload);
	return read.success
		? { ok: true, data: read.output }
		: { ok: false, status: 502, detail: `${upstream} contract drift: ${v.summarize(read.issues)}` };
}
