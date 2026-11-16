// A stand-in OIDC issuer in each of the two shapes the workbench signs in at (`providers.ts`), served same-origin under
// `/oidc` (Keycloak-shaped) and `/oidc-dex` (Dex-shaped); the e2e edge passes the browser's host in `x-forwarded-host`
// and the prefix it was reached under in `x-forwarded-prefix`, so the discovery document names that origin and that
// issuer. The app's real sign-in path runs against it end to end: discovery, the authorization-code redirect with PKCE,
// the code exchange (with a DPoP proof where the shape binds tokens), the refresh, and the sign-out. No spec needs an
// auth bypass in the app.
//
// WHO SIGNS IN is the spec's choice: `signIn()` (session.ts) sets the `mock_oidc_identity` cookie, and
// the authorize step carries that identity inside the code it hands back, so the exchange is stateless
// and parallel specs never share an identity. With no cookie the issuer shows a login page and stops.
// `mock_oidc_lifetime` (seconds) shortens the tokens it issues, for a spec about renewal.
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

import { mintToken, readToken, verifyProof, type TokenClaims } from './dpop';
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

const json = (body: unknown, status = 200): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** RFC 7636 S256: the base64url SHA-256 of the verifier. */
const s256 = (verifier: string): string =>
	new Bun.CryptoHasher('sha256').update(verifier).digest('base64url');

/** An OAuth error, as the token endpoint answers one. */
const refused = (error: string, description: string): Response =>
	json({ error, error_description: description }, 400);

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
		const origin = `http://${req.headers.get('x-forwarded-host') ?? req.headers.get('host')}`;
		const shape: Shape =
			req.headers.get('x-forwarded-prefix') === SHAPES.dex.prefix ? 'dex' : 'keycloak';
		const issuer = `${origin}${SHAPES[shape].prefix}`;

		if (url.pathname === '/.well-known/openid-configuration') {
			return json({
				issuer,
				authorization_endpoint: `${issuer}/auth`,
				token_endpoint: `${issuer}/token`,
				jwks_uri: `${issuer}/keys`,
				...(shape === 'keycloak' && !cookieOf(req, NO_END_SESSION_COOKIE)
					? { end_session_endpoint: `${issuer}/logout` }
					: {}),
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
		if (url.pathname === '/keys') return json({ keys: [] });

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
			if (!identity || params.get('prompt') === 'login') {
				return new Response('<!doctype html><title>Mock IdP</title><h1>Mock IdP login</h1>', {
					headers: { 'content-type': 'text/html' },
				});
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

		if (url.pathname === '/logout') {
			return new Response('<!doctype html><title>Mock IdP</title><h1>Mock IdP signed out</h1>', {
				headers: { 'content-type': 'text/html' },
			});
		}
		return json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
	},
});

console.log(`mock OIDC issuer on :${MOCK_OIDC_PORT}`);
