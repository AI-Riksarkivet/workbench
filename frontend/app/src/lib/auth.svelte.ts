/**
 * Sign-in, in the browser: OpenID Connect authorization code with PKCE against the platform's issuer,
 * as the workbench's own PUBLIC client, at whatever OIDC provider the deployment names.
 *
 * The app has no backend, so no secret exists to hold: the browser proves possession of the code with
 * the PKCE verifier and keeps the tokens IN MEMORY (`InMemoryWebStorage`). Only the transient sign-in
 * state (the PKCE verifier and the return path, for one redirect) rides `sessionStorage`. A reload
 * starts signed out and goes back to the issuer.
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
import { InMemoryWebStorage, User, UserManager, WebStorageStateStore } from 'oidc-client-ts';
import type { DPoPState, OidcMetadata, UserManagerSettings } from 'oidc-client-ts';
import * as v from 'valibot';
import { readConfig } from './config.ts';
import { setSigner, type Signer } from './api/client.ts';
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
}

export const auth = new AuthState();

let manager: UserManager | null = null;
/** The user oidc-client-ts answered, held for signing; never rendered. */
let user: User | null = null;
let ready: Promise<void> | null = null;
/** The renewal in flight, which every caller that finds the session expired waits on. */
let renewing: Promise<User | null> | null = null;

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
 *  session within `RENEW_MARGIN_SECONDS` of its expiry is renewed first where `renew` can, and nobody held, or a
 *  session that has expired, sends the request without credentials. */
export function sessionSigner(
	oidc: UserManager,
	held: () => User | null,
	renew: () => Promise<User | null>,
): Signer {
	return async (htu, method) => {
		let current = held();
		if (current && due(current) && current.refresh_token) current = (await renew()) ?? current;
		if (!current || current.expired) return null;
		if (current.token_type.toLowerCase() !== 'dpop')
			return { token: current.access_token, proof: null };
		const proof = await oidc.dpopProof(htu, current, method);
		return proof ? { token: current.access_token, proof } : null;
	};
}

/** Renew the session with its refresh token, once however many callers ask at the same time; null when there is none
 *  to renew with, or the provider refused. */
function renew(): Promise<User | null> {
	renewing ??= (async () => {
		try {
			if (!manager || !user?.refresh_token) return null;
			const fresh = await manager.signinSilent();
			if (!fresh) return null;
			user = fresh;
			auth.session = toSession(fresh);
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
		setSigner(sessionSigner(manager, () => user, renew));
		auth.enabled = true;
	})();
	return ready;
}

/** The sign-in route's parameter that asks the issuer to authenticate again rather than answer from a
 *  session it keeps. */
export const REAUTHENTICATE = { param: 'prompt', value: 'login' } as const;

/** The sign-in this page has sent the browser on, while it is in flight. */
let leaving: Promise<void> | null = null;

/** Send the browser to the issuer; it comes back to `/auth/callback` and then to `returnTo`.
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
export function login(returnTo: string, { reauthenticate = false } = {}): Promise<void> {
	if (leaving) return leaving;
	const started = redirect(returnTo, reauthenticate).finally(() => {
		if (leaving === started) leaving = null;
	});
	leaving = started;
	return started;
}

/** A page restored from the back/forward cache has no sign-in in flight: the one it sent was abandoned by Back. */
function restored(event: PageTransitionEvent): void {
	if (event.persisted) leaving = null;
}
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function')
	window.addEventListener('pageshow', restored);

async function redirect(returnTo: string, reauthenticate: boolean): Promise<void> {
	await initAuth();
	if (!manager) return;
	// A sign-in this tab started that never came back is the one case worth asking the provider about its client: it
	// shows its own error page for a client it does not know, and the person comes back here by the browser's Back.
	if (signInPending()) {
		const unknown = await clientUnknown(manager);
		if (unknown) {
			auth.configError = unknown;
			return;
		}
	}
	markSignIn(true);
	// Settles only when the browser did not leave, or came back to this page by Back (see `login`).
	await manager.signinRedirect({
		state: { returnTo },
		...(reauthenticate ? { prompt: REAUTHENTICATE.value } : {}),
	});
}

/** Where this tab notes a sign-in it sent to the provider, until the provider sends it back. */
const PENDING_SIGN_IN = 'workbench.sign-in-pending';

function markSignIn(pending: boolean): void {
	try {
		if (pending) sessionStorage.setItem(PENDING_SIGN_IN, '1');
		else sessionStorage.removeItem(PENDING_SIGN_IN);
	} catch {
		// No storage: the provider's own page is what the person sees for a client it does not know.
	}
}

function signInPending(): boolean {
	try {
		return sessionStorage.getItem(PENDING_SIGN_IN) !== null;
	} catch {
		return false;
	}
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
	return `the identity provider does not know this app's client "${oidc.settings.client_id}": register it there (a development estate: \`make dev-dex-client\`)`;
}

/** The session has ended: renew it in place where it can be, or send the person to sign in again and come back to
 *  `returnTo`. */
export async function renewOrSignIn(returnTo: string): Promise<void> {
	if (await renew()) return;
	await login(returnTo);
}

/** Finish the redirect: exchange the code (with the PKCE verifier, and a DPoP proof where the session is bound) and
 *  hold the tokens in memory. Answers the same-origin path the person was on when sent to sign in. */
export async function completeLogin(): Promise<string> {
	await initAuth();
	if (!manager) return '/';
	user = await manager.signinRedirectCallback();
	markSignIn(false);
	auth.session = toSession(user);
	const state = user.state as { returnTo?: unknown } | undefined;
	return sameOriginPath(typeof state?.returnTo === 'string' ? state.returnTo : '/');
}

/** End the session here, and at the issuer when it advertises an end-session endpoint.
 *
 *  `signoutRedirect` reads the held user's id token to send as `id_token_hint` (which session to end)
 *  and removes the user, and with it any DPoP key, before it navigates; removing the user first would
 *  send the request without the hint. An issuer with no end-session endpoint (Dex) offers no way to end
 *  its session from here, so the browser goes to sign-in with `prompt=login` and whoever comes next must
 *  authenticate. */
export async function logout(): Promise<void> {
	await initAuth();
	auth.session = null;
	user = null;
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
