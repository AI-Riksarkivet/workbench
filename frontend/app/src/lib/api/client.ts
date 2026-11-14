// #lib/api/client — the browser's one transport to the workbench's services.
//
// The app is a single-page app with no backend of its own: every call leaves the browser for this
// origin's `/api/*`, which the platform's Gateway routes to the service that owns the prefix
// (`/config.json`'s `services`). Each service verifies the caller on every request, so nothing in between
// holds a credential.
//
// THE TOKEN IS BOUND TO THIS TAB'S KEY (DPoP, RFC 9449). Every request carries `Authorization: DPoP
// <token>` and a `DPoP` proof signed for exactly that request: its method (`htm`) and its URL without
// query or fragment (`htu`, § 4.2; the services compare it so, `service_kit.governed.dpop`). One proof
// per request, never reused: each carries a fresh `jti`, which a service remembers against a replay.
// oidc-client-ts signs `htu` exactly as it is given (3.5.0's `generateDPoPProof` puts the url argument in
// the claim unchanged), so the query and fragment are stripped HERE, before the signer sees the URL.
//
// The proof comes from a SIGNER the app installs once (`setSigner`, from `auth.svelte.ts`), so this
// module holds no token and no key, and stays testable without a browser.
import * as v from 'valibot';
import type { WriteKey } from '../write-key.svelte.ts';

/** Per-request timeout for a read: a hung service must not stack reads. */
const FETCH_TIMEOUT_MS = 8000;

/** A read that carries a file (an image or a voice clip to search by) uploads it and runs its encoder
 *  first, which the first call after a service restart loads: given longer than a plain read. */
const UPLOAD_TIMEOUT_MS = 30_000;

/** A write's timeout: a write given up on may land anyway, so it is waited for, and a page tells the
 *  person only that it is NOT CONFIRMED when no answer came. */
const WRITE_TIMEOUT_MS = 120_000;

/** What a request is sent with: the token and the proof signed for it. */
export interface Credentials {
	token: string;
	proof: string;
}

/** Signs one request: `htu` is already stripped of query and fragment. Null when nobody is signed in
 *  (or the stack signs nobody in): the request then goes without credentials. */
export type Signer = (htu: string, method: string) => Promise<Credentials | null>;

let signer: Signer = async () => null;

/** Install the source of credentials (the app's in-memory OIDC session and its DPoP key). */
export function setSigner(next: Signer): void {
	signer = next;
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

/** `fetch` with this tab's DPoP credentials, signed for this one request, and only to this origin. */
export async function authorizedFetch(input: string, init: RequestInit = {}): Promise<Response> {
	const headers = new Headers(init.headers);
	const method = (init.method ?? 'GET').toUpperCase();
	const htu = htuOf(input);
	if (htu !== null && !headers.has('authorization')) {
		const credentials = await signer(htu, method);
		if (credentials) {
			headers.set('authorization', `DPoP ${credentials.token}`);
			headers.set('dpop', credentials.proof);
		}
	}
	return fetch(input, { ...init, method, headers });
}

// AbortSignal.timeout needs Safari 16+/Chrome 103+; vite transpiles syntax, not APIs.
function timeoutSignal(ms: number = FETCH_TIMEOUT_MS): AbortSignal {
	if (typeof AbortSignal.timeout === 'function') return AbortSignal.timeout(ms);
	const controller = new AbortController();
	setTimeout(() => controller.abort(), ms);
	return controller.signal;
}

/** {@link authorizedFetch} given up after a read's timeout: the fetch the explorer's client calls
 *  through (`@workbench/explorer-api`'s transport), so none of its reads can hang a panel. A request
 *  with a body is a read that uploads, given {@link UPLOAD_TIMEOUT_MS}. */
export function timedFetch(input: string, init: RequestInit = {}): Promise<Response> {
	const uploads = init.body !== undefined && init.body !== null;
	return authorizedFetch(input, {
		...init,
		signal: init.signal ?? timeoutSignal(uploads ? UPLOAD_TIMEOUT_MS : FETCH_TIMEOUT_MS),
	});
}

/** A fetch outcome that keeps the HTTP status — `status: 0` is UNREACHABLE (no answer came), `n` is an
 *  answer with the service's `detail`, and `502` is contract drift (a body that fails its schema). */
export type ApiResult<T> = { ok: true; data: T } | { ok: false; status: number; detail: string };

/** Lift `detail` out of an error body (FastAPI's `{detail}` and problem+json's `detail` alike). */
function detailFrom(body: unknown, fallback: string): string {
	if (body !== null && typeof body === 'object' && 'detail' in body) {
		const detail = (body as { detail: unknown }).detail;
		return typeof detail === 'string' ? detail : JSON.stringify(detail);
	}
	return fallback;
}

async function failure(res: Response, upstream: string): Promise<ApiResult<never>> {
	const body: unknown = await res.json().catch(() => ({}));
	return {
		ok: false,
		status: res.status,
		detail: detailFrom(body, `${upstream} answered ${res.status}`),
	};
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
		res = await authorizedFetch(url, { ...init, signal: timeoutSignal() });
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

/** The binary sibling of {@link readJSON}: the response's bytes (an Arrow IPC stream) and its ETag. */
export async function readBytes(
	url: string,
	upstream: string,
	init: RequestInit = {},
): Promise<ApiResult<{ bytes: Uint8Array; etag: string | null }>> {
	let res: Response;
	try {
		res = await authorizedFetch(url, { ...init, signal: timeoutSignal() });
	} catch (err) {
		return { ok: false, status: 0, detail: `${upstream} is unreachable (${String(err)})` };
	}
	if (!res.ok) return failure(res, upstream);
	return {
		ok: true,
		data: { bytes: new Uint8Array(await res.arrayBuffer()), etag: res.headers.get('etag') },
	};
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
	options: { method?: string; key?: WriteKey } = {},
): Promise<ApiResult<T>> {
	const text = JSON.stringify(body);
	const result = await sendWrite(
		url,
		text,
		schema,
		upstream,
		options.method ?? 'POST',
		options.key?.take(text),
	);
	options.key?.settle(result);
	return result;
}

async function sendWrite<T>(
	url: string,
	text: string,
	schema: v.GenericSchema<unknown, T>,
	upstream: string,
	method: string,
	idempotencyKey: string | undefined,
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
			signal: timeoutSignal(WRITE_TIMEOUT_MS),
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
