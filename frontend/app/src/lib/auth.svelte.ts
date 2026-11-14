/**
 * Sign-in, in the browser: OpenID Connect authorization code with PKCE against the platform's issuer,
 * as the workbench's own PUBLIC client, with the tokens bound to this tab by DPoP (RFC 9449).
 *
 * The app has no backend, so no secret exists to hold: the browser proves possession of the code with
 * the PKCE verifier, keeps the tokens IN MEMORY (`InMemoryWebStorage`), and keeps the DPoP key pair in
 * memory too (`MemoryDPoPStore`). oidc-client-ts 3.5.0 generates that pair with
 * `crypto.subtle.generateKey({name: 'ECDSA', namedCurve: 'P-256'}, false, ...)`: the private key is
 * NOT EXTRACTABLE, so a script injected into the page can sign with it while the page lives but cannot
 * carry it away, and a token it steals is useless without it (RFC 10017 § 6.1). The key is made at the
 * code exchange, which the library signs with it, so the issuer binds the token to it (`cnf.jkt`).
 * Tokens and key die with the tab together; a stored key would outlive tokens nobody holds.
 * Only the transient sign-in state (the PKCE verifier and the return path, for one redirect) rides
 * `sessionStorage`. A reload starts signed out and goes back to the issuer.
 *
 * The issuer, client, scopes and the services' public bases come from `/config.json` (`config.ts`).
 */
import { InMemoryWebStorage, User, UserManager, WebStorageStateStore } from 'oidc-client-ts';
import type { DPoPState, UserManagerSettings } from 'oidc-client-ts';
import { readConfig } from './config.ts';
import { setSigner, type Signer } from './api/client.ts';
import { setServices } from './api/services.ts';
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

/** The signer of `held()`'s requests: its bound token and a fresh proof for each `htu` and `method`,
 *  signed by `oidc` with the key it holds for the client. Nobody held, or an expired token: no
 *  credentials, and the request goes without. */
export function dpopSigner(oidc: UserManager, held: () => User | null): Signer {
	return async (htu, method) => {
		const current = held();
		if (!current || current.expired) return null;
		const proof = await oidc.dpopProof(htu, current, method);
		return proof ? { token: current.access_token, proof } : null;
	};
}

/** Read the config once and build the OIDC client. Idempotent; every caller awaits the same read. */
export function initAuth(fetch: typeof globalThis.fetch = globalThis.fetch): Promise<void> {
	ready ??= (async () => {
		const read = await readConfig(fetch);
		if ('error' in read) {
			auth.configError = read.error;
			return;
		}
		if (read.off) {
			setServices(read.services);
			return;
		}
		const config = read.config;
		setServices(config.services);
		manager = new UserManager({
			authority: new URL(config.issuer, location.origin).href.replace(/\/$/, ''),
			client_id: config.clientId,
			redirect_uri: `${location.origin}/auth/callback`,
			post_logout_redirect_uri: `${location.origin}/`,
			response_type: 'code',
			scope: config.scopes.join(' '),
			userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
			stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
			dpop: { store: new MemoryDPoPStore() },
			loadUserInfo: false,
			automaticSilentRenew: false,
			monitorSession: false,
		});
		setSigner(dpopSigner(manager, () => user));
		auth.enabled = true;
	})();
	return ready;
}

/** The sign-in route's parameter that asks the issuer to authenticate again rather than answer from a
 *  session it keeps. */
export const REAUTHENTICATE = { param: 'prompt', value: 'login' } as const;

/** Send the browser to the issuer; it comes back to `/auth/callback` and then to `returnTo`. */
export async function login(returnTo: string, { reauthenticate = false } = {}): Promise<void> {
	await initAuth();
	if (!manager) return;
	await manager.signinRedirect({
		state: { returnTo },
		...(reauthenticate ? { prompt: REAUTHENTICATE.value } : {}),
	});
}

/** Finish the redirect: exchange the code (with the PKCE verifier and a DPoP proof) and hold the
 *  tokens in memory. Answers the same-origin path the person was on when sent to sign in. */
export async function completeLogin(): Promise<string> {
	await initAuth();
	if (!manager) return '/';
	user = await manager.signinRedirectCallback();
	auth.session = toSession(user);
	const state = user.state as { returnTo?: unknown } | undefined;
	return sameOriginPath(typeof state?.returnTo === 'string' ? state.returnTo : '/');
}

/** End the session here, and at the issuer when it advertises an end-session endpoint.
 *
 *  `signoutRedirect` reads the held user's id token to send as `id_token_hint` (which session to end)
 *  and removes the user, and with it the DPoP key, before it navigates; removing the user first would
 *  send the request without the hint. An issuer with no end-session endpoint offers no way to end its
 *  session from here, so the browser goes to sign-in with `prompt=login` and whoever comes next must
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

/** Whether the held token has expired (a token with no expiry claim never does). */
export function expired(session: Session, nowSeconds = Math.floor(Date.now() / 1000)): boolean {
	return session.expiresAt > 0 && session.expiresAt <= nowSeconds;
}
