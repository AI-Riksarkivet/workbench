// A stand-in OIDC issuer, served same-origin under `/oidc` (the e2e edge passes the browser's host in
// `x-forwarded-host`, so the discovery document names that origin). The app's real sign-in path runs
// against it end to end: discovery, the authorization-code redirect with PKCE, and the token exchange
// with a DPoP proof. No spec needs an auth bypass in the app.
//
// WHO SIGNS IN is the spec's choice: `signIn()` (session.ts) sets the `mock_oidc_identity` cookie, and
// the authorize step carries that identity inside the code it hands back, so the exchange is stateless
// and parallel specs never share an identity. With no cookie the issuer shows a login page and stops.
//
// THE EXCHANGE IS HELD TO WHAT A PROVIDER ENFORCES for the workbench's public client: `/auth` refuses
// another client, a `redirect_uri` other than the app's one callback, and a request without an S256
// challenge; `/token` refuses a verifier that does not hash to the challenge, another client or
// redirect URI, and A REQUEST WITHOUT A VALID DPoP PROOF for `POST <issuer>/token` (RFC 9449 § 5). The
// access token it answers is bound to that proof's key (`cnf.jkt`, § 6.1) and typed `DPoP`, so the mock
// services refuse it with any other key, as Keycloak's DPoP-bound tokens are refused.

import { mintToken, verifyProof } from './dpop';
import { MOCK_OIDC_PORT } from './ports';

/** What a spec signs in as. */
export interface MockIdentity {
	sub: string;
	name: string;
	email: string;
}

const COOKIE = 'mock_oidc_identity';
/** Set by a spec: discovery then advertises no end-session endpoint. */
const NO_END_SESSION_COOKIE = 'mock_oidc_no_end_session';
/** The workbench's own public client, as `e2e/app-config.json` names it. */
const CLIENT_ID = 'workbench-app';

/** What one authorization code stands for, carried inside the code itself. */
interface Grant {
	identity: MockIdentity;
	clientId: string;
	redirectUri: string;
	challenge: string;
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

Bun.serve({
	port: MOCK_OIDC_PORT,
	async fetch(req) {
		const url = new URL(req.url);
		const origin = `http://${req.headers.get('x-forwarded-host') ?? req.headers.get('host')}`;
		const issuer = `${origin}/oidc`;

		if (url.pathname === '/.well-known/openid-configuration') {
			return json({
				issuer,
				authorization_endpoint: `${issuer}/auth`,
				token_endpoint: `${issuer}/token`,
				jwks_uri: `${issuer}/keys`,
				...(cookieOf(req, NO_END_SESSION_COOKIE)
					? {}
					: { end_session_endpoint: `${issuer}/logout` }),
				response_types_supported: ['code'],
				subject_types_supported: ['public'],
				id_token_signing_alg_values_supported: ['none'],
				dpop_signing_alg_values_supported: ['ES256'],
				code_challenge_methods_supported: ['S256'],
			});
		}
		if (url.pathname === '/keys') return json({ keys: [] });

		if (url.pathname === '/auth') {
			const params = url.searchParams;
			if (params.get('client_id') !== CLIENT_ID) return json({ error: 'unauthorized_client' }, 400);
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
				clientId: CLIENT_ID,
				redirectUri: callback,
				challenge: params.get('code_challenge') ?? '',
			};
			const back = new URL(grant.redirectUri);
			back.searchParams.set('code', b64url(JSON.stringify(grant)));
			back.searchParams.set('state', params.get('state') ?? '');
			return new Response(null, { status: 302, headers: { location: back.href } });
		}

		if (url.pathname === '/token' && req.method === 'POST') {
			const form = new URLSearchParams(await req.text());
			if (form.get('grant_type') !== 'authorization_code') {
				return refused('unsupported_grant_type', 'only the authorization code grant');
			}
			let grant: Grant;
			try {
				grant = JSON.parse(unb64url(form.get('code') ?? '')) as Grant;
			} catch {
				return refused('invalid_grant', 'unknown code');
			}
			if (form.get('client_id') !== grant.clientId) {
				return refused('invalid_client', 'the code was issued to another client');
			}
			if (form.get('redirect_uri') !== grant.redirectUri) {
				return refused('invalid_grant', 'redirect_uri differs from the authorize request');
			}
			const verifier = form.get('code_verifier');
			if (!verifier || s256(verifier) !== grant.challenge) {
				return refused('invalid_grant', 'code_verifier does not match the code_challenge');
			}
			let jkt: string;
			try {
				jkt = (await verifyProof(req.headers.get('dpop'), 'POST', `${issuer}/token`)).jkt;
			} catch (err) {
				return refused('invalid_dpop_proof', String(err));
			}
			const who = grant.identity;
			const now = Math.floor(Date.now() / 1000);
			const claims = {
				iss: issuer,
				aud: grant.clientId,
				sub: who.sub,
				name: who.name,
				email: who.email,
				iat: now,
				exp: now + 3600,
			};
			const idToken = `${b64url('{"alg":"none","typ":"JWT"}')}.${b64url(JSON.stringify(claims))}.`;
			return json({
				access_token: mintToken({
					sub: who.sub,
					name: who.name,
					email: who.email,
					cnf: { jkt },
					exp: now + 3600,
				}),
				id_token: idToken,
				token_type: 'DPoP',
				expires_in: 3600,
			});
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
