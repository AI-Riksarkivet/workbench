/**
 * Sign-in, in the browser: OpenID Connect authorization code with PKCE against the platform's issuer,
 * as the workbench's own PUBLIC client, at whatever OIDC provider the deployment names.
 *
 * The app has no backend, so no secret exists to hold: the browser proves possession of the code with
 * the PKCE verifier and keeps the tokens IN MEMORY (`InMemoryWebStorage`). Only the transient sign-in
 * state (the PKCE verifier and the return path, for one redirect) rides `sessionStorage`. A reload
 * starts with no tokens and goes back to the issuer (ADR 0020).
 *
 * A RELOAD KEEPS THE SESSION THROUGH THE ISSUER'S OWN, where the deployment allows it (`/config.json`'s
 * `silentSignIn`, on by default; off, no sign-in sends `prompt=none`). The first sign-in a tab sends asks with `prompt=none`
 * (OIDC Core § 3.1.2.1), by a top-level redirect: an issuer that holds the person's login session answers with a code
 * and no form, and one that holds none answers `login_required` (or another error) at the callback, which sends the
 * same request again ONCE, interactively (`completeLogin`). A top-level redirect, not a hidden iframe: the issuer's
 * session cookie rides a top-level navigation under any third-party-cookie policy (`SameSite=Lax` included), and the
 * issuer need allow no framing. Not a stored refresh token: nothing that can be presented as the person is written to
 * browser storage. What is stored holds no credential: `SILENT_TRIED` (this tab asked silently and has not signed in
 * since, so the next sign-in shows the form: an issuer that answers `prompt=none` with an error page of its own, or
 * shows its form anyway, never loops), and the sign-out record, `SIGNED_OUT` and `ANSWERED` (below `REAUTHENTICATE`).
 *
 * WHAT THE PROVIDER OFFERS DECIDES THE REST, read from its discovery document before anything is asked:
 *
 * - DPoP (RFC 9449), where discovery advertises `dpop_signing_alg_values_supported` with ES256 (the
 *   algorithm oidc-client-ts 3.5.0 signs with) and `/config.json` does not say `dpop: "off"`; where it says
 *   `dpop: "required"` (the services require a proof), a provider that does not offer it is refused. The key
 *   pair is then made at the code exchange, which the library signs with it, so the issuer binds the
 *   token to it (`cnf.jkt`), and kept in memory (`MemoryDPoPStore`). oidc-client-ts generates it with
 *   `crypto.subtle.generateKey({name: 'ECDSA', namedCurve: 'P-256'}, false, ...)`: the private key is
 *   NOT EXTRACTABLE, so a script injected into the page can sign with it while the page lives but
 *   cannot carry it away, and a token it steals is useless without it (RFC 10017 § 6.1). Tokens and key
 *   die with the tab together.
 * - Otherwise (Dex advertises no DPoP, measured on the dev Dex v2.46.1 2026-11-16) the token is a plain
 *   bearer held in memory, and a request presents it under the scheme its `token_type` names.
 * - Renewal: with a refresh token (the provider's own, or Dex's for `offline_access`), an expired session
 *   is renewed in place (`signinSilent`, which oidc-client-ts sends with a DPoP proof of the same key
 *   when the session is bound); without one, or when the provider refuses, the person signs in again.
 *
 * The issuer, client, scopes and the services' public bases come from `/config.json` (`config.ts`).
 */
import {
	ErrorResponse,
	InMemoryWebStorage,
	User,
	UserManager,
	WebStorageStateStore,
} from 'oidc-client-ts';
import type { DPoPState, OidcMetadata, UserManagerSettings } from 'oidc-client-ts';
import * as v from 'valibot';
import { readConfig } from './config.ts';
import { ENDED, setSigner, type Signer } from './api/client.ts';
import { setServices } from './api/services.ts';
import { setCanvasSettings } from './canvas/settings.ts';
import { sameOriginPath } from './return-path.ts';

type DPoPStore = NonNullable<UserManagerSettings['dpop']>['store'];

/** The DPoP key pair, held for the page's life and no longer. */
export class MemoryDPoPStore implements DPoPStore {
	readonly #held = new Map<string, DPoPState>();
	async set(key: string, value: DPoPState): Promise<void> {
		this.#held.set(key, value);
	}
	// The library's declared contract answers a state for any key; it reads `get` only after
	// `getAllKeys` says the key is held, or tests the answer for undefined.
	async get(key: string): Promise<DPoPState> {
		return this.#held.get(key) as DPoPState;
	}
	async remove(key: string): Promise<DPoPState> {
		const held = this.#held.get(key);
		this.#held.delete(key);
		return held as DPoPState;
	}
	async getAllKeys(): Promise<string[]> {
		return [...this.#held.keys()];
	}
}

/** The signed-in person, as the app shows them. */
export interface Session {
	sub: string;
	name: string;
	email: string | null;
	/** Epoch seconds; 0 when the token names no expiry. */
	expiresAt: number;
}

class AuthState {
	/** Whether this stack signs people in at all. */
	enabled = $state(false);
	/** Why `/config.json` could not be used, when it could not: the app then runs nothing. */
	configError = $state<string | null>(null);
	session = $state.raw<Session | null>(null);
	/** Whether this tab holds tokens for `session`: false once they are dropped (a sign-out here or in another tab),
	 *  while the session shown stays until the person signs in again, as an ended one does. */
	tokensHeld = $state(false);
}

export const auth = new AuthState();

let manager: UserManager | null = null;
/** The user oidc-client-ts answered, held for signing; never rendered. */
let user: User | null = null;
let ready: Promise<void> | null = null;
/** The renewal in flight, which every caller that finds the session expired waits on. */
let renewing: Promise<User | null> | null = null;
/** The session's generation: bumped whenever this tab drops its tokens (`dropTokens`). A renewal or a signature reads it
 *  when it starts and again when its await settles, and keeps nothing from an older generation: one that straddled a
 *  sign-out ends with no token, never the one from before it. (A sign-in is judged by the sign-out record instead,
 *  `completeLogin`, which sees every drop: each one follows a new sign-out id.) */
let generation = 0;

/** Drop this tab's tokens: nothing that started before signs or installs one after (`generation`), and the expiry
 *  timer stops (`auth.tokensHeld`). The library's own store is the caller's to clear, after it read what it needs. */
function dropTokens(): void {
	generation++;
	user = null;
	auth.tokensHeld = false;
}

/** Hold `fresh` as this tab's session. */
function hold(fresh: User): void {
	user = fresh;
	auth.session = toSession(fresh);
	auth.tokensHeld = true;
}

function toSession(held: User): Session {
	const claims = held.profile;
	const email = typeof claims.email === 'string' ? claims.email : null;
	return {
		sub: claims.sub,
		name: String(claims.name ?? claims.preferred_username ?? email ?? claims.sub),
		email,
		expiresAt: held.expires_at ?? 0,
	};
}

/** How long before its expiry a session is renewed, so a request signed on it does not arrive after it lapsed. */
export const RENEW_MARGIN_SECONDS = 2;

/** Whether `held` is close enough to its expiry to be renewed before it is used. */
const due = (held: User): boolean => (held.expires_in ?? Infinity) <= RENEW_MARGIN_SECONDS;

/** The User Timing mark a request's signer sets when it waits on a renewal of the session. */
const SIGNER_WAITS = 'workbench.signer-waits-for-renewal';

/** The algorithm oidc-client-ts signs its DPoP proofs with. */
const DPOP_ALGORITHM = 'ES256';

/** The part of the provider's discovery document the app decides by; the rest is handed to oidc-client-ts. */
const DiscoverySchema = v.looseObject({
	issuer: v.string(),
	authorization_endpoint: v.string(),
	token_endpoint: v.string(),
	dpop_signing_alg_values_supported: v.optional(v.array(v.string()), []),
});

/** The provider's discovery document, and whether it binds tokens with an algorithm the app signs; or why it could not
 *  be read. */
async function discover(
	fetch: typeof globalThis.fetch,
	authority: string,
): Promise<{ metadata: Partial<OidcMetadata>; dpop: boolean } | { error: string }> {
	const url = `${authority}/.well-known/openid-configuration`;
	let body: unknown;
	try {
		const res = await fetch(url, { headers: { accept: 'application/json' } });
		if (!res.ok) return { error: `${url} answered ${res.status}` };
		body = await res.json();
	} catch (err) {
		return { error: `${url} could not be read: ${String(err)}` };
	}
	const read = v.safeParse(DiscoverySchema, body);
	if (!read.success)
		return { error: `${url} is not a discovery document: ${v.summarize(read.issues)}` };
	// RFC 8414 § 3.3: the document's issuer is the one asked for, or none of its endpoints may be used.
	if (read.output.issuer.replace(/\/$/, '') !== authority) {
		return { error: `${url} names the issuer ${read.output.issuer}, not ${authority}` };
	}
	return {
		metadata: read.output as Partial<OidcMetadata>,
		dpop: read.output.dpop_signing_alg_values_supported.includes(DPOP_ALGORITHM),
	};
}

/** The signer of `held()`'s requests. A session bound by DPoP presents its token with a fresh proof for each `htu` and
 *  `method`, signed by `oidc` with the key it holds for the client; a bearer session presents its token alone. A
 *  session within `RENEW_MARGIN_SECONDS` of its expiry is renewed first where `renew` can. Nobody held sends the
 *  request without credentials; a session that has expired and was not renewed (a tab left in the background, whose
 *  expiry timer the browser held back) sends it without them as an ENDED session, which asks the person to sign in
 *  again (`#lib/reauth.svelte.ts`). */
export function sessionSigner(
	oidc: UserManager,
	held: () => User | null,
	renew: () => Promise<User | null>,
	generationOf: () => number = () => 0,
): Signer {
	return async (htu, method) => {
		const at = generationOf();
		const before = held();
		if (before && due(before) && before.refresh_token) {
			// A User Timing mark, so a reader of the page's timeline (and the suite) can see a request wait on a renewal.
			performance.clearMarks(SIGNER_WAITS);
			performance.mark(SIGNER_WAITS);
			await renew();
		}
		// What is held after the wait, never what was held before it: a sign-out meanwhile has left nobody held
		// (`dropTokens`), and nothing can hold another before this resumes (a renewal starts only with someone held, and
		// a sign-in completes only on the callback's own page load), so the generation needs no check here.
		const current = held();
		if (!current) return null;
		if (current.expired) return ENDED;
		if (current.token_type.toLowerCase() !== 'dpop')
			return { token: current.access_token, proof: null };
		const proof = await oidc.dpopProof(htu, current, method);
		if (generationOf() !== at) return null;
		return proof ? { token: current.access_token, proof } : null;
	};
}

/** Renew the session with its refresh token, once however many callers ask at the same time; null when there is none
 *  to renew with, or the provider refused. */
function renew(): Promise<User | null> {
	renewing ??= (async () => {
		const at = generation;
		try {
			if (!manager || !user?.refresh_token) return null;
			const fresh = await manager.signinSilent();
			if (at !== generation) {
				// Dropped while the issuer answered: the tokens it gave (which the library has just stored) are not kept.
				await manager.removeUser();
				return null;
			}
			if (!fresh) return null;
			hold(fresh);
			return fresh;
		} catch {
			return null;
		} finally {
			renewing = null;
		}
	})();
	return renewing;
}

/** Read the config and the provider's discovery once, and build the OIDC client. Idempotent; every caller awaits the
 *  same read. */
export function initAuth(fetch: typeof globalThis.fetch = globalThis.fetch): Promise<void> {
	ready ??= (async () => {
		const read = await readConfig(fetch);
		if ('error' in read) {
			auth.configError = read.error;
			return;
		}
		if (read.off) {
			setServices(read.services);
			setCanvasSettings(read.canvas);
			return;
		}
		const config = read.config;
		silentAllowed = config.silentSignIn;
		setServices(config.services);
		setCanvasSettings(config.canvas);
		const authority = new URL(config.issuer, location.origin).href.replace(/\/$/, '');
		const provider = await discover(fetch, authority);
		if ('error' in provider) {
			auth.configError = `the issuer's discovery document: ${provider.error}`;
			return;
		}
		if (config.dpop === 'required' && !provider.dpop) {
			auth.configError = `this deployment requires DPoP-bound tokens, and the issuer's discovery offers no ${DPOP_ALGORITHM} DPoP`;
			return;
		}
		manager = new UserManager({
			authority,
			metadata: provider.metadata,
			client_id: config.clientId,
			redirect_uri: `${location.origin}/auth/callback`,
			post_logout_redirect_uri: `${location.origin}/`,
			response_type: 'code',
			scope: config.scopes.join(' '),
			userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
			stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
			...(config.dpop !== 'off' && provider.dpop ? { dpop: { store: new MemoryDPoPStore() } } : {}),
			loadUserInfo: false,
			automaticSilentRenew: false,
			monitorSession: false,
		});
		setSigner(
			sessionSigner(
				manager,
				() => user,
				renew,
				() => generation,
			),
		);
		auth.enabled = true;
	})();
	return ready;
}

/** The sign-in route's parameter that asks the issuer to authenticate again rather than answer from a
 *  session it keeps. */
export const REAUTHENTICATE = { param: 'prompt', value: 'login' } as const;

/** This tab asked the issuer for a sign-in without prompting and has not signed in since (`sessionStorage`). */
const SILENT_TRIED = 'workbench.silent-sign-in-tried';
/**
 * THE SIGN-OUT RECORD, in `localStorage` so every tab of the origin reads the same, and never a credential: two random
 * ids that name nobody and prove nothing.
 *
 * - `SIGNED_OUT` holds the id of the LAST sign-out in this browser, written by `logout` and only ever overwritten by the
 *   next one, never removed.
 * - `ANSWERED` holds the id of the sign-out the last completed sign-in answered (empty for none), written by
 *   `completeLogin`.
 *
 * The rules, and the only places that read them:
 * - SIGNED OUT is: the last sign-out's id differs from the answered one (a missing value reads as empty). Then no
 *   sign-in is silent and each sends `prompt=login` (`redirect`). A first visit (neither value) is not signed out.
 * - A sign-in records in its OIDC state the last sign-out's id when it was sent (`after`). At its callback it is held
 *   only when the last sign-out's id is still that one, and then writes `ANSWERED` = that id; otherwise its tokens are
 *   dropped unused and the person is asked again with `prompt=login`, whatever happened in between (another tab's
 *   sign-in included: nothing is ever removed, so a sign-out cannot pass unseen).
 * - A change of the last sign-out's id (a new value, unlike the old) drops the tokens of every other tab that sees it
 *   (`signedOutElsewhere`); a change of `ANSWERED` drops nothing.
 *
 * Writes from two tabs cannot undo a sign-out: two sign-ins writing `ANSWERED` for the same id write the same value; a
 * sign-in that recorded an older id finds a newer one at its callback and is discarded without writing; and a sign-out
 * written between a sign-in's check and its write of `ANSWERED` leaves the last id unlike the answered one, so the
 * browser stays signed out.
 *
 * Storage that refuses a read reads as no value, which is "not signed out": that errs toward a silent sign-in only where
 * the same storage could not have recorded the sign-out either, the one case where a sign-out is not remembered past its
 * page (and only the issuer's end-session ends it).
 */
const SIGNED_OUT = 'workbench.signed-out';
const ANSWERED = 'workbench.signed-in-after';

/** The last sign-out's id in this browser, empty for none. */
const lastSignOut = (): string => noted(browser, SIGNED_OUT) ?? '';

/** Whether the person signed out in this browser and has not signed in since. */
const signedOutHere = (): boolean => lastSignOut() !== (noted(browser, ANSWERED) ?? '');
/** Where this tab notes a sign-in it sent to the provider, until the provider sends it back. */
const PENDING_SIGN_IN = 'workbench.sign-in-pending';

/** Note `value` under `key` in `store`, or remove it (null). */
function note(store: () => Storage, key: string, value: string | null): void {
	try {
		if (value === null) store().removeItem(key);
		else store().setItem(key, value);
	} catch {
		// Storage refused: without this tab's sessionStorage no sign-in completes at all (its PKCE state lives there);
		// without localStorage a sign-out is not remembered past this page, and only its issuer's end-session ends it.
	}
}

/** What `store` holds under `key`, or null. */
function noted(store: () => Storage, key: string): string | null {
	try {
		return store().getItem(key);
	} catch {
		return null;
	}
}

const tab = (): Storage => sessionStorage;
const browser = (): Storage => localStorage;

/** Whether the deployment lets a sign-in ask the issuer without prompting (`/config.json`'s `silentSignIn`). */
let silentAllowed = true;

/** The `prompt` a sign-in sends: `login` when asked to authenticate again or after a sign-out; `none` where the
 *  deployment allows it and this tab has not yet asked silently since it last signed in; none otherwise (the issuer's
 *  form, or its session where it keeps one). */
function promptFor(asked: {
	reauthenticate: boolean;
	signedOut: boolean;
	silentTried: boolean;
}): typeof REAUTHENTICATE.value | 'none' | undefined {
	if (asked.reauthenticate || asked.signedOut) return REAUTHENTICATE.value;
	return silentAllowed && !asked.silentTried ? 'none' : undefined;
}

/** What a sign-in rides through the issuer in the OIDC `state` (kept in this tab's state store, never in the URL): where
 *  to come back to, whether it was asked without prompting, and the last sign-out's id when it was sent (empty for
 *  none). */
interface SignInState {
	returnTo: string;
	silent?: boolean;
	after: string;
}

function stateOf(state: unknown): Partial<SignInState> {
	return typeof state === 'object' && state !== null ? (state as Partial<SignInState>) : {};
}

/** The sign-in this page has sent the browser on, while it is in flight. */
let leaving: Promise<void> | null = null;

/** Send the browser to the issuer; it comes back to `/auth/callback` and then to `returnTo`. `replace` leaves in place
 *  of the current history entry. `fresh` is the person's own choice (the prompt's "Sign in again") and starts a new
 *  sign-in even while one is noted in flight: a leave the person cancelled (the browser's "Leave site?" answered Stay)
 *  settles nothing, so the one it started would otherwise hold every later sign-in of this page.
 *
 *  ONE sign-in in flight per page: a second call while the first is leaving (a read refused 401 while the session's
 *  own renewal gives up, the canvas's save and its page at once) joins the first. Each call would otherwise store a
 *  PKCE state of its own and redirect again, and the second, finding the first's sign-in pending, would ask the token
 *  endpoint about the client with a code no provider issued (`clientUnknown`), which a provider answers 400.
 *
 *  The sign-in is in flight until its redirect settles: oidc-client-ts's redirect resolves on `pageshow`, when the
 *  person comes back by Back to this page restored from the back/forward cache, and rejects when it fails. Either way
 *  the next call starts a new one; so does a page restored from the cache (`pageshow` with `persisted`), whatever the
 *  library does. */
export function login(
	returnTo: string,
	{ reauthenticate = false, replace = false, fresh = false } = {},
): Promise<void> {
	if (leaving && !fresh) return leaving;
	const started = redirect(returnTo, reauthenticate, replace).finally(() => {
		if (leaving === started) leaving = null;
	});
	leaving = started;
	return started;
}

/** A page restored from the back/forward cache has no sign-in in flight: the one it sent was abandoned by Back. A page
 *  with nobody signed in was waiting for that sign-in and shows nothing, so it is loaded again, and its sign-in starts
 *  over (asked of the person: the silent one was tried). */
function restored(event: PageTransitionEvent): void {
	if (!event.persisted || !leaving) return;
	leaving = null;
	if (!auth.session) location.reload();
}

/** Told when another tab of this browser signed out (`#lib/reauth.svelte.ts` raises the shell's one prompt). */
let signedOutWatcher: () => void = () => undefined;

export function setSignedOutWatcher(next: () => void): void {
	signedOutWatcher = next;
}

/** Another tab of this browser signed out: this tab drops its tokens, so no request it sends from now on carries them
 *  (the signer holds nobody), and the person is asked by the shell's one prompt, as for a session that ended. Nothing
 *  navigates on its own: the page keeps what it shows, and the person's "Sign in again" keeps what the page asks to keep
 *  and comes back to the same address, with `prompt=login` (`SIGNED_OUT`). The session shown stays until then, as an
 *  ended one does. */
function signedOutElsewhere(event: StorageEvent): void {
	if (
		event.storageArea !== localStorage ||
		event.key !== SIGNED_OUT ||
		event.newValue === null ||
		event.newValue === event.oldValue
	)
		return;
	const held = user !== null;
	dropTokens();
	void manager?.removeUser();
	if (held) signedOutWatcher();
}

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
	window.addEventListener('pageshow', restored);
	window.addEventListener('storage', signedOutElsewhere);
}

async function redirect(
	returnTo: string,
	reauthenticate: boolean,
	replace: boolean,
): Promise<void> {
	await initAuth();
	if (!manager) return;
	// A sign-in this tab started that never came back is the one case worth asking the provider about its client: it
	// shows its own error page for a client it does not know, and the person comes back here by the browser's Back.
	if (noted(tab, PENDING_SIGN_IN) !== null) {
		const unknown = await clientUnknown(manager);
		if (unknown) {
			auth.configError = unknown;
			return;
		}
	}
	const after = lastSignOut();
	const prompt = promptFor({
		reauthenticate,
		signedOut: signedOutHere(),
		silentTried: noted(tab, SILENT_TRIED) !== null,
	});
	if (prompt === 'none') note(tab, SILENT_TRIED, '1');
	note(tab, PENDING_SIGN_IN, '1');
	const state: SignInState = { returnTo, after, ...(prompt === 'none' ? { silent: true } : {}) };
	// Settles only when the browser did not leave, or came back to this page by Back (see `login`).
	await manager.signinRedirect({
		state,
		...(prompt ? { prompt } : {}),
		...(replace ? { redirectMethod: 'replace' as const } : {}),
	});
}

/** Why the provider will not sign anyone in to this app's client, or null when it may.
 *
 *  A provider that does not know the client shows its own error page at the authorization endpoint and never comes back
 *  (it cannot trust a redirect URI it holds for no client), so the app asks first: a code exchange with a code no
 *  provider issued is refused `invalid_client` (RFC 6749 § 5.2, 401) for a client the provider does not hold, and for
 *  any other reason for one it does (Dex v2.46.1 authenticates the client before it reads the code). Any other answer,
 *  or none, leaves the sign-in to go ahead. */
async function clientUnknown(oidc: UserManager): Promise<string | null> {
	const endpoint = await oidc.metadataService.getTokenEndpoint().catch(() => undefined);
	if (!endpoint) return null;
	const nonce = crypto.randomUUID();
	let answer: Response;
	try {
		answer = await fetch(endpoint, {
			method: 'POST',
			headers: { 'content-type': 'application/x-www-form-urlencoded' },
			body: new URLSearchParams({
				grant_type: 'authorization_code',
				code: `not-a-code-${nonce}`,
				code_verifier: `not-a-verifier-${nonce}-${nonce}`,
				client_id: oidc.settings.client_id,
				redirect_uri: oidc.settings.redirect_uri,
			}),
		});
	} catch {
		return null;
	}
	if (answer.status !== 401) return null;
	const body: unknown = await answer.json().catch(() => null);
	const error = typeof body === 'object' && body !== null && 'error' in body ? body.error : null;
	if (error !== 'invalid_client') return null;
	return `the identity provider does not know this app's client "${oidc.settings.client_id}": register it there (a development estate: the lakehouse's install carries it, docs/DEPLOY.md)`;
}

/** The session is ending: renew it in place where it can be; false when it could not be, and the person is then asked to
 *  sign in again (`#lib/reauth.svelte.ts`), never sent away on their own. */
export async function renewSession(): Promise<boolean> {
	return (await renew()) !== null;
}

/** The same-origin return path a sign-in's state names, or `/`. */
function returnPathOf(state: unknown): string {
	const { returnTo } = stateOf(state);
	return sameOriginPath(typeof returnTo === 'string' ? returnTo : '/');
}

/** Finish the redirect: exchange the code (with the PKCE verifier, and a DPoP proof where the session is bound) and
 *  hold the tokens in memory. Answers the same-origin path the person was on when sent to sign in, or null when the
 *  browser is leaving for the issuer again, to the same address, interactively:
 *
 *  - a sign-in asked without prompting that the issuer answered with an error (no session there, or one it will not use
 *    without the person). oidc-client-ts reads that error only after it matched the response's `state` to one this tab
 *    stored, so a callback another site forged is refused and sends nobody anywhere.
 *  - a sign-in that left before the person signed out in this browser (the last sign-out's id is not the one it
 *    recorded, the sign-out record above): its tokens are dropped unused, and the person is asked again with
 *    `prompt=login`. */
export async function completeLogin(): Promise<string | null> {
	await initAuth();
	if (!manager) return '/';
	let fresh: User;
	try {
		fresh = await manager.signinRedirectCallback();
	} catch (err) {
		if (!(err instanceof ErrorResponse) || stateOf(err.state).silent !== true) throw err;
		// The issuer came back: it knows this client, so the next sign-in is not one that never returned.
		note(tab, PENDING_SIGN_IN, null);
		// In place of the callback's own entry, which Back must not return to: its state is spent.
		await login(returnPathOf(err.state), { replace: true });
		return null;
	}
	note(tab, PENDING_SIGN_IN, null);
	// Every way this tab's tokens are dropped writes a new sign-out id first (`logout`), or follows one another tab
	// wrote (`signedOutElsewhere`), and none is ever removed, so comparing ids sees every drop since this sign-in left.
	const answers = lastSignOut();
	if (answers !== (stateOf(fresh.state).after ?? '')) {
		await manager.removeUser();
		await login(returnPathOf(fresh.state), { replace: true, reauthenticate: true });
		return null;
	}
	hold(fresh);
	note(tab, SILENT_TRIED, null);
	note(browser, ANSWERED, answers);
	return returnPathOf(fresh.state);
}

/** End the session here, and at the issuer when it advertises an end-session endpoint (OpenID Connect RP-Initiated
 *  Logout 1.0), which sends the browser back to the app's root.
 *
 *  `signoutRedirect` reads the held user's id token to send as `id_token_hint` (which session to end)
 *  and removes the user, and with it any DPoP key, before it navigates; removing the user first would
 *  send the request without the hint. The id token lives in oidc-client-ts's in-memory user store until then, and
 *  leaves the page only in that one request's URL, to its own issuer. An issuer with no end-session endpoint (Dex
 *  without its sessions) offers no way to end its session from here, so the browser goes to sign-in with
 *  `prompt=login`. Either way a new sign-out id is written to `SIGNED_OUT` first (the sign-out record above): until the
 *  person signs in again no sign-in in this browser is silent and each asks the issuer to authenticate them, so a reload
 *  never signs them back in from a session the issuer kept. */
export async function logout(): Promise<void> {
	await initAuth();
	dropTokens();
	auth.session = null;
	note(browser, SIGNED_OUT, crypto.randomUUID());
	if (!manager) {
		location.assign('/');
		return;
	}
	const metadata = await manager.metadataService.getMetadata().catch(() => null);
	if (metadata?.end_session_endpoint) {
		await manager.signoutRedirect();
		return;
	}
	await manager.removeUser();
	const next = new URLSearchParams({ [REAUTHENTICATE.param]: REAUTHENTICATE.value });
	location.assign(`/auth/login?${next}`);
}
