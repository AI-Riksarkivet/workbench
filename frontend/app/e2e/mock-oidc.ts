// A stand-in OIDC issuer in each of the two shapes the workbench signs in at (`providers.ts`), served same-origin under
// `/oidc` (Keycloak-shaped) and `/oidc-dex` (Dex-shaped); the e2e edge passes the browser's host in `x-forwarded-host`
// and the prefix it was reached under in `x-forwarded-prefix`, so the discovery document names that origin and that
// issuer. The app's real sign-in path runs against it end to end: discovery, the authorization-code redirect with PKCE,
// the code exchange (with a DPoP proof where the shape binds tokens), the refresh, and the sign-out. No spec needs an
// auth bypass in the app.
//
// WHO SIGNS IN is the spec's choice: `signIn()` (session.ts) sets the `mock_oidc_identity` cookie, and
// the authorize step carries that identity inside the code it hands back, so the exchange is stateless
// and parallel specs never share an identity. `mock_oidc_lifetime` (seconds) shortens the tokens it issues, for a spec
// about renewal.
//
// A LOGIN SESSION, AS EACH REAL ISSUER KEEPS ONE OR NOT. Where the issuer keeps one (Keycloak-shaped; Dex-shaped with
// `mock_oidc_dex_sessions=on`, as Dex v2.46 with `DEX_SESSIONS_ENABLED`), the identity cookie IS that session: an
// authorization request is answered from it without a form, `prompt=none` without one is answered
// `error=login_required` at the client's callback (OIDC Core § 3.1.2.1, § 3.1.2.6), `prompt=login` shows the login page
// whatever it holds, and discovery advertises the end-session endpoint (Keycloak-shaped unless
// `mock_oidc_no_end_session`). Where it keeps none (Dex-shaped by default, as Dex v2.46.1 without its sessions flag),
// EVERY request shows the login form and its `prompt` is not acted on there (v2.46.1 `server/authflow/login.go:102`),
// the identity cookie is what the person types, and discovery names no end-session endpoint (`/logout` is 404). After
// the form that Dex decides consent (`server/authflow/dispatch.go`): with `mock_oidc_dex_approval=screen`
// (`skipApprovalScreen: false`, Dex's default, whose consent is never stored without sessions) a request that carried
// `prompt=none` is answered `error=interaction_required` and any other shows the approval page; without it
// (`skipApprovalScreen: true`) the code is issued. The login page's `Sign in` (and the approval page's `Grant access`)
// send the same request on, as a person at the form does. `mock_oidc_prompt_none=error-page` makes the Keycloak shape
// a provider that answers `prompt=none` with an error page of its own and never returns to the client.
//
// The end-session endpoint ends the session (the cookie is cleared) only on a request it accepts: an `id_token_hint`
// this issuer signed, and no `post_logout_redirect_uri` or the one the client registered (the app's root), which it
// then redirects back to. Any other is refused with an error page and the session is left as it was.
//
// THE EXCHANGE IS HELD TO WHAT A PROVIDER ENFORCES for the workbench's public client: `/auth` refuses
// another client, a `redirect_uri` other than the app's one callback, and a request without an S256
// challenge; `/token` refuses a verifier that does not hash to the challenge, another client or
// redirect URI. Keycloak-shaped, it also refuses A REQUEST WITHOUT A VALID DPoP PROOF for `POST <issuer>/token`
// (RFC 9449 § 5), and the access token it answers is bound to that proof's key (`cnf.jkt`, § 6.1) and typed `DPoP`, so
// the mock services refuse it with any other key; a refresh must be proved by the same key. Dex-shaped, the token is a
// `bearer` whose audiences are the clients its scopes ask for beside the client itself, with `federated_claims` for
// `federated:id`, and a refresh token only for `offline_access`.
//
// The services' own calls (`mock-lakehouse.ts`) reach `/token` as the workbench's confidential client: an RFC 8693
// exchange of a person's token (Keycloak-shaped only; Dex refuses one of its own tokens) and the client-credentials
// grant. `GET /__grants?sub=` answers the grants issued for one person, in order.
//
// THE SIGNING KEY IS PER PERSON, so a spec can restart the issuer under one person alone: a person's tokens name the key
// `/keys?sub=` publishes for them (`FIRST_KEY` until a spec says otherwise), and `POST /__rotate?sub=&kid=` replaces it,
// as an issuer that restarted with a new key and none of its old ones. `POST /__keys-fail?sub=&times=n` makes the next
// n reads of that person's `/keys` answer 503, as an issuer still coming up does.

import { FIRST_KEY, mintToken, readToken, verifyProof, type TokenClaims } from './dpop';
import { MOCK_OIDC_PORT } from './ports';
import {
	APP_CLIENT,
	LAKEHOUSE_AUDIENCE,
	SERVICE_CLIENT,
	SERVICE_SECRET,
	SHAPES,
	audienceScope,
	type Shape,
} from './providers';

/** What a spec signs in as. */
export interface MockIdentity {
	sub: string;
	name: string;
	email: string;
}

const COOKIE = 'mock_oidc_identity';
/** Set by a spec: discovery then advertises no end-session endpoint. */
const NO_END_SESSION_COOKIE = 'mock_oidc_no_end_session';
/** Set by a spec: how many seconds each token lives. */
const LIFETIME_COOKIE = 'mock_oidc_lifetime';
/** Set by a spec: the Keycloak shape answers `prompt=none` with an error page of its own (`error-page`). */
const PROMPT_NONE_COOKIE = 'mock_oidc_prompt_none';
/** Set by a spec: the Dex shape keeps sessions (`on`), as Dex with `DEX_SESSIONS_ENABLED`. */
const DEX_SESSIONS_COOKIE = 'mock_oidc_dex_sessions';
/** Set by a spec: the Dex shape shows its approval screen (`screen`), as Dex with `skipApprovalScreen: false`. */
const DEX_APPROVAL_COOKIE = 'mock_oidc_dex_approval';
/** The interactive login page's own continuation of the request it was shown for. */
const SIGNED_IN_AT_FORM = 'mock_signed_in';
/** The approval page's own continuation. */
const APPROVED_AT_FORM = 'mock_approved';
const DEFAULT_LIFETIME = 3600;
const TOKEN_EXCHANGE = 'urn:ietf:params:oauth:grant-type:token-exchange';
const ACCESS_TOKEN_TYPE = 'urn:ietf:params:oauth:token-type:access_token';

/** What one authorization code stands for, carried inside the code itself; a refresh token carries the same, and the
 *  key the session is bound to. */
interface Grant {
	identity: MockIdentity;
	clientId: string;
	redirectUri: string;
	challenge: string;
	scopes: string[];
	lifetime: number;
	jkt?: string;
}

const b64url = (text: string): string => Buffer.from(text, 'utf8').toString('base64url');
const unb64url = (text: string): string => Buffer.from(text, 'base64url').toString('utf8');

function cookieOf(req: Request, name: string): string | null {
	for (const part of (req.headers.get('cookie') ?? '').split(';')) {
		const [key, ...value] = part.trim().split('=');
		if (key === name) return decodeURIComponent(value.join('='));
	}
	return null;
}

const page = (heading: string, body = '', status = 200): Response =>
	new Response(`<!doctype html><title>Mock IdP</title><h1>${heading}</h1>${body}`, {
		status,
		headers: { 'content-type': 'text/html' },
	});

const escapeHtml = (text: string): string =>
	text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

/** A page whose one button sends the authorization request on, marked with `marks`. */
function formPage(
	heading: string,
	button: string,
	issuer: string,
	params: URLSearchParams,
	marks: string[],
): Response {
	const fields = [...params]
		.filter(([key]) => !marks.includes(key))
		.map(
			([key, value]) =>
				`<input type="hidden" name="${escapeHtml(key)}" value="${escapeHtml(value)}">`,
		)
		.concat(marks.map((mark) => `<input type="hidden" name="${mark}" value="1">`))
		.join('');
	return page(
		heading,
		`<form method="get" action="${escapeHtml(`${issuer}/auth`)}">${fields}<button>${button}</button></form>`,
	);
}

/** The login page for an authorization request: its `Sign in` sends the same request again, from the form. */
const loginPage = (issuer: string, params: URLSearchParams): Response =>
	formPage('Mock IdP login', 'Sign in', issuer, params, [SIGNED_IN_AT_FORM]);

/** Dex's approval screen: its `Grant access` sends the request on, signed in and approved. */
const approvalPage = (issuer: string, params: URLSearchParams): Response =>
	formPage('Mock IdP grant access', 'Grant access', issuer, params, [
		SIGNED_IN_AT_FORM,
		APPROVED_AT_FORM,
	]);

/** The session cookie, ended. */
const ENDED_SESSION = `${COOKIE}=; Path=/; Max-Age=0`;

const json = (body: unknown, status = 200): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** RFC 7636 S256: the base64url SHA-256 of the verifier. */
const s256 = (verifier: string): string =>
	new Bun.CryptoHasher('sha256').update(verifier).digest('base64url');

/** An OAuth error, as the token endpoint answers one. */
const refused = (error: string, description: string): Response =>
	json({ error, error_description: description }, 400);

const published = new Map<string, string>();
const keyOf = (sub: string): string => published.get(sub) ?? FIRST_KEY;
const keysFailing = new Map<string, number>();

const grants = new Map<string, string[]>();
function issued(sub: string, grant: string): void {
	grants.set(sub, [...(grants.get(sub) ?? []), grant]);
}

/** The audiences a person's token names: the workbench's own (Keycloak's audience mapper), or the clients its scopes
 *  ask for beside the client itself (Dex). */
function audiencesOf(shape: Shape, grant: Grant): string[] {
	if (shape === 'keycloak') return [SHAPES.keycloak.workbenchAudience];
	const prefix = audienceScope('');
	const asked = grant.scopes
		.filter((scope) => scope.startsWith(prefix))
		.map((scope) => scope.slice(prefix.length));
	return [...new Set([...asked, grant.clientId])];
}

/** The tokens a grant is answered with, by `shape`'s issuer. */
function tokensFor(shape: Shape, issuer: string, grant: Grant): Response {
	const who = grant.identity;
	const now = Math.floor(Date.now() / 1000);
	const aud = audiencesOf(shape, grant);
	const claims: TokenClaims = {
		iss: issuer,
		aud,
		sub: who.sub,
		name: who.name,
		email: who.email,
		kid: keyOf(who.sub),
		exp: now + grant.lifetime,
		...(grant.jkt ? { cnf: { jkt: grant.jkt } } : {}),
		...(shape === 'dex' && aud.length > 1 ? { azp: grant.clientId } : {}),
		...(shape === 'dex' && grant.scopes.includes('federated:id')
			? { federated_claims: { connector_id: 'local', user_id: who.sub } }
			: {}),
	};
	const idClaims = {
		iss: issuer,
		aud: grant.clientId,
		sub: who.sub,
		name: who.name,
		email: who.email,
		iat: now,
		exp: now + grant.lifetime,
	};
	const refreshes = shape === 'keycloak' || grant.scopes.includes('offline_access');
	return json({
		access_token: mintToken(claims),
		id_token: `${b64url('{"alg":"none","typ":"JWT"}')}.${b64url(JSON.stringify(idClaims))}.`,
		token_type: grant.jkt ? 'DPoP' : 'bearer',
		expires_in: grant.lifetime,
		...(refreshes ? { refresh_token: `refresh.${b64url(JSON.stringify(grant))}` } : {}),
	});
}

/** The confidential client's Basic credentials, as the token endpoint checks them. */
const SERVICE_BASIC = `Basic ${Buffer.from(`${SERVICE_CLIENT}:${SERVICE_SECRET}`).toString('base64')}`;

/** A grant the workbench's services ask for as their confidential client: an exchange, or their own token. */
function serviceGrant(shape: Shape, issuer: string, form: URLSearchParams, req: Request): Response {
	if (req.headers.get('authorization') !== SERVICE_BASIC)
		return json({ error: 'invalid_client' }, 401);
	const now = Math.floor(Date.now() / 1000);
	if (form.get('grant_type') === 'client_credentials') {
		const scoped = (form.get('scope') ?? '').split(' ').includes(audienceScope(LAKEHOUSE_AUDIENCE));
		const aud =
			shape === 'keycloak'
				? [form.get('audience') ?? SERVICE_CLIENT]
				: scoped
					? [LAKEHOUSE_AUDIENCE, SERVICE_CLIENT]
					: [SERVICE_CLIENT];
		const sub = shape === 'keycloak' ? `service-account-${SERVICE_CLIENT}` : SERVICE_CLIENT;
		const token = mintToken({
			iss: issuer,
			aud,
			sub,
			name: SERVICE_CLIENT,
			email: '',
			exp: now + 300,
		});
		return json({ access_token: token, token_type: 'bearer', expires_in: 300 });
	}
	// Dex exchanges a token only for the upstream connector `connector_id` names, never one of its own.
	if (shape === 'dex') return refused('invalid_request', 'Requested connector does not exist.');
	const subject = readToken(form.get('subject_token') ?? '');
	if (
		form.get('subject_token_type') !== ACCESS_TOKEN_TYPE ||
		!subject ||
		subject.iss !== issuer ||
		subject.exp < now ||
		!subject.aud.includes(SHAPES.keycloak.workbenchAudience)
	)
		return refused('invalid_grant', 'the subject token is not one this issuer gave the workbench');
	if (form.get('audience') !== LAKEHOUSE_AUDIENCE)
		return refused('invalid_target', 'unknown audience');
	issued(subject.sub, 'token-exchange');
	const token = mintToken({
		iss: issuer,
		aud: [LAKEHOUSE_AUDIENCE],
		sub: subject.sub,
		name: subject.name,
		email: subject.email,
		exp: now + 300,
	});
	return json({
		access_token: token,
		token_type: 'Bearer',
		issued_token_type: ACCESS_TOKEN_TYPE,
		expires_in: 300,
	});
}

/** The claims of an id token this issuer signed (`alg: none`), or null. */
function readIdToken(token: string): { iss: string; sub: string } | null {
	try {
		const claims = JSON.parse(unb64url(token.split('.')[1] ?? '')) as {
			iss?: unknown;
			sub?: unknown;
		};
		return typeof claims.iss === 'string' && typeof claims.sub === 'string'
			? { iss: claims.iss, sub: claims.sub }
			: null;
	} catch {
		return null;
	}
}

/** The code or refresh token a token request redeems, read back into its grant; null for one this issuer never gave. */
function redeemed(type: string | null, form: URLSearchParams): Grant | null {
	try {
		if (type === 'authorization_code') return JSON.parse(unb64url(form.get('code') ?? '')) as Grant;
		const refresh = form.get('refresh_token') ?? '';
		if (type === 'refresh_token' && refresh.startsWith('refresh.'))
			return JSON.parse(unb64url(refresh.slice('refresh.'.length))) as Grant;
	} catch {
		return null;
	}
	return null;
}

Bun.serve({
	port: MOCK_OIDC_PORT,
	async fetch(req) {
		const url = new URL(req.url);
		if (url.pathname === '/__grants')
			return json(grants.get(url.searchParams.get('sub') ?? '') ?? []);
		if (url.pathname === '/__rotate' && req.method === 'POST') {
			published.set(url.searchParams.get('sub') ?? '', url.searchParams.get('kid') ?? '');
			return json({ ok: true });
		}
		if (url.pathname === '/__keys-fail' && req.method === 'POST') {
			keysFailing.set(
				url.searchParams.get('sub') ?? '',
				Number(url.searchParams.get('times') ?? 1),
			);
			return json({ ok: true });
		}
		const origin = `http://${req.headers.get('x-forwarded-host') ?? req.headers.get('host')}`;
		const shape: Shape =
			req.headers.get('x-forwarded-prefix') === SHAPES.dex.prefix ? 'dex' : 'keycloak';
		const issuer = `${origin}${SHAPES[shape].prefix}`;
		const sessionKept = shape === 'keycloak' || cookieOf(req, DEX_SESSIONS_COOKIE) === 'on';
		const endSession = sessionKept && !cookieOf(req, NO_END_SESSION_COOKIE);

		if (url.pathname === '/.well-known/openid-configuration') {
			return json({
				issuer,
				authorization_endpoint: `${issuer}/auth`,
				token_endpoint: `${issuer}/token`,
				jwks_uri: `${issuer}/keys`,
				...(endSession ? { end_session_endpoint: `${issuer}/logout` } : {}),
				response_types_supported: ['code'],
				subject_types_supported: ['public'],
				id_token_signing_alg_values_supported: ['none'],
				grant_types_supported: [
					'authorization_code',
					'client_credentials',
					'refresh_token',
					TOKEN_EXCHANGE,
				],
				...(shape === 'keycloak' ? { dpop_signing_alg_values_supported: ['ES256'] } : {}),
				code_challenge_methods_supported: ['S256'],
			});
		}
		if (url.pathname === '/keys') {
			const sub = url.searchParams.get('sub');
			const failing = sub === null ? 0 : (keysFailing.get(sub) ?? 0);
			if (sub !== null && failing > 0) {
				keysFailing.set(sub, failing - 1);
				return json({ error: 'the issuer is starting' }, 503);
			}
			return json({ keys: sub === null ? [] : [{ kid: keyOf(sub) }] });
		}

		if (url.pathname === '/auth') {
			const params = url.searchParams;
			if (params.get('client_id') !== APP_CLIENT)
				return json({ error: 'unauthorized_client' }, 400);
			const callback = `${origin}/auth/callback`;
			if (params.get('redirect_uri') !== callback) {
				return refused('invalid_request', `redirect_uri is not the registered ${callback}`);
			}
			if (params.get('code_challenge_method') !== 'S256' || !params.get('code_challenge')) {
				return refused('invalid_request', 'PKCE S256 is required');
			}
			const identity = cookieOf(req, COOKIE);
			const atForm = params.get(SIGNED_IN_AT_FORM) === '1';
			const prompts = (params.get('prompt') ?? '').split(' ');
			const answer = (error: string): Response => {
				const back = new URL(callback);
				back.searchParams.set('error', error);
				back.searchParams.set('state', params.get('state') ?? '');
				return new Response(null, { status: 302, headers: { location: back.href } });
			};
			if (sessionKept) {
				if (!atForm && prompts.includes('none') && !identity) {
					if (cookieOf(req, PROMPT_NONE_COOKIE) === 'error-page')
						return page('Mock IdP error', '<p>prompt=none is not supported</p>', 400);
					return answer('login_required');
				}
				if (!identity || (!atForm && prompts.includes('login'))) return loginPage(issuer, params);
			} else {
				if (!atForm || !identity) return loginPage(issuer, params);
				if (
					cookieOf(req, DEX_APPROVAL_COOKIE) === 'screen' &&
					params.get(APPROVED_AT_FORM) !== '1'
				) {
					if (prompts.includes('none')) return answer('interaction_required');
					return approvalPage(issuer, params);
				}
			}
			const grant: Grant = {
				identity: JSON.parse(identity) as MockIdentity,
				clientId: APP_CLIENT,
				redirectUri: callback,
				challenge: params.get('code_challenge') ?? '',
				scopes: (params.get('scope') ?? '').split(' ').filter(Boolean),
				lifetime: Number(cookieOf(req, LIFETIME_COOKIE) ?? DEFAULT_LIFETIME),
			};
			const back = new URL(grant.redirectUri);
			back.searchParams.set('code', b64url(JSON.stringify(grant)));
			back.searchParams.set('state', params.get('state') ?? '');
			return new Response(null, { status: 302, headers: { location: back.href } });
		}

		if (url.pathname === '/token' && req.method === 'POST') {
			const form = new URLSearchParams(await req.text());
			const type = form.get('grant_type');
			if (type === TOKEN_EXCHANGE || type === 'client_credentials')
				return serviceGrant(shape, issuer, form, req);
			if (type !== 'authorization_code' && type !== 'refresh_token')
				return refused('unsupported_grant_type', `no ${type} grant`);
			// The client is authenticated before the grant is read (Dex v2.46.1 `authenticateClient`).
			if (form.get('client_id') !== APP_CLIENT)
				return json(
					{ error: 'invalid_client', error_description: 'Invalid client credentials.' },
					401,
				);
			let grant = redeemed(type, form);
			if (!grant) return refused('invalid_grant', 'unknown code or refresh token');
			if (form.get('client_id') !== grant.clientId) {
				return refused('invalid_client', 'the grant was issued to another client');
			}
			if (type === 'authorization_code') {
				if (form.get('redirect_uri') !== grant.redirectUri) {
					return refused('invalid_grant', 'redirect_uri differs from the authorize request');
				}
				const verifier = form.get('code_verifier');
				if (!verifier || s256(verifier) !== grant.challenge) {
					return refused('invalid_grant', 'code_verifier does not match the code_challenge');
				}
			}
			if (shape === 'keycloak') {
				let jkt: string;
				try {
					jkt = (await verifyProof(req.headers.get('dpop'), 'POST', `${issuer}/token`)).jkt;
				} catch (err) {
					return refused('invalid_dpop_proof', String(err));
				}
				// A refresh is proved by the key the session was bound to at the code exchange (RFC 9449 § 5).
				if (type === 'refresh_token' && jkt !== grant.jkt)
					return refused('invalid_dpop_proof', 'the refresh is proved by another key');
				grant = { ...grant, jkt };
			}
			issued(grant.identity.sub, type);
			return tokensFor(shape, issuer, grant);
		}

		if (url.pathname === '/logout' && endSession) {
			const hint = readIdToken(url.searchParams.get('id_token_hint') ?? '');
			const back = url.searchParams.get('post_logout_redirect_uri');
			if (!hint || hint.iss !== issuer)
				return page('Mock IdP refused sign-out', '<p>Invalid id_token_hint.</p>', 400);
			if (back !== null && back !== `${origin}/`)
				return page(
					'Mock IdP refused sign-out',
					'<p>Unregistered post_logout_redirect_uri.</p>',
					400,
				);
			if (back !== null)
				return new Response(null, {
					status: 302,
					headers: { location: back, 'set-cookie': ENDED_SESSION },
				});
			const ended = page('Mock IdP signed out');
			ended.headers.set('set-cookie', ENDED_SESSION);
			return ended;
		}
		return json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
	},
});

console.log(`mock OIDC issuer on :${MOCK_OIDC_PORT}`);
