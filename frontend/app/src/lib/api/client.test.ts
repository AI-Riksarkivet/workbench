import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { User, UserManager, WebStorageStateStore, InMemoryWebStorage } from 'oidc-client-ts';
import { MemoryDPoPStore, dpopSigner } from '../auth.svelte.ts';
import { authorizedFetch, htuOf, setSigner, writeJSON } from './client.ts';
import { WriteKey } from '../write-key.svelte.ts';
import * as v from 'valibot';

// The one fetch seam, signed by the real oidc-client-ts 3.5.0 with its own DPoP key: what goes on the
// wire for each request.

const ORIGIN = 'https://workbench.example';

const claimsOf = (jwt: string): Record<string, unknown> =>
	JSON.parse(Buffer.from(jwt.split('.')[1] ?? '', 'base64url').toString('utf8'));

/** The requests the seam sent, with their headers. */
let sent: { url: string; method: string; headers: Headers }[] = [];

beforeEach(() => {
	sent = [];
	// oidc-client-ts reaches the Web Crypto API through `window.crypto`.
	vi.stubGlobal('window', globalThis);
	vi.stubGlobal('location', new URL(`${ORIGIN}/datasets`));
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string, init: RequestInit) => {
			sent.push({ url, method: init.method ?? 'GET', headers: new Headers(init.headers) });
			return Response.json({ ok: true });
		}),
	);
});

afterEach(() => {
	vi.unstubAllGlobals();
	setSigner(async () => null);
});

/** A signed-in tab: the library's key in an in-memory store, and a user holding a bound token. */
async function signedIn(): Promise<{ store: MemoryDPoPStore }> {
	const store = new MemoryDPoPStore();
	const oidc = new UserManager({
		authority: `${ORIGIN}/oidc`,
		client_id: 'workbench-app',
		redirect_uri: `${ORIGIN}/auth/callback`,
		userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
		stateStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
		dpop: { store },
	});
	await oidc.generateDPoPJkt({ store });
	const user = new User({
		access_token: 'the-bound-token',
		token_type: 'DPoP',
		profile: { sub: 'ada', iss: `${ORIGIN}/oidc`, aud: 'workbench-app', exp: 0, iat: 0 },
		expires_at: Math.floor(Date.now() / 1000) + 600,
	});
	setSigner(dpopSigner(oidc, () => user));
	return { store };
}

describe('the DPoP fetch seam', () => {
	it('signs htu as this origin and path, with no query or fragment, and htm as the method', async () => {
		await signedIn();

		await authorizedFetch('/api/search/api/search?dataset=acme%24letters&q=harbour#top');
		await authorizedFetch('/api/viewer/api/signed-media', { method: 'post', body: '{}' });

		const [search, sign] = sent;
		expect(search?.headers.get('authorization')).toBe('DPoP the-bound-token');
		expect(claimsOf(search?.headers.get('dpop') ?? '')).toMatchObject({
			htm: 'GET',
			htu: `${ORIGIN}/api/search/api/search`,
		});
		expect(claimsOf(sign?.headers.get('dpop') ?? '')).toMatchObject({
			htm: 'POST',
			htu: `${ORIGIN}/api/viewer/api/signed-media`,
		});
	});

	it('signs one fresh proof per request, bound to the token by ath', async () => {
		await signedIn();

		await authorizedFetch('/api/viewer/api/datasets');
		await authorizedFetch('/api/viewer/api/datasets');

		const [first, second] = sent.map((req) => claimsOf(req.headers.get('dpop') ?? ''));
		expect(first?.jti).not.toBe(second?.jti);
		const ath = Buffer.from(
			await crypto.subtle.digest('SHA-256', new TextEncoder().encode('the-bound-token')),
		).toString('base64url');
		expect(first?.ath).toBe(ath);
	});

	it('keeps the key in memory, its private half not extractable', async () => {
		const { store } = await signedIn();

		const [key] = await store.getAllKeys();
		const held = await store.get(key ?? '');
		expect(held.keys.privateKey.extractable).toBe(false);
		await expect(crypto.subtle.exportKey('jwk', held.keys.privateKey)).rejects.toThrow();
	});

	it('hands no credential to another origin', async () => {
		await signedIn();

		await authorizedFetch('https://elsewhere.example/api/viewer/api/datasets');

		expect(sent[0]?.headers.has('authorization')).toBe(false);
		expect(sent[0]?.headers.has('dpop')).toBe(false);
		expect(htuOf('//elsewhere.example/x', ORIGIN)).toBeNull();
	});
});

describe('a write', () => {
	it('that got no answer is NOT CONFIRMED, and its retry keeps the Idempotency-Key', async () => {
		const key = new WriteKey();
		vi.stubGlobal(
			'fetch',
			vi.fn(async (url: string, init: RequestInit) => {
				sent.push({ url, method: init.method ?? 'GET', headers: new Headers(init.headers) });
				throw new TypeError('network error');
			}),
		);

		const first = await writeJSON('/api/annotator/x', { a: 1 }, v.unknown(), 'the annotator', {
			key,
		});
		await writeJSON('/api/annotator/x', { a: 1 }, v.unknown(), 'the annotator', { key });

		expect(first).toMatchObject({ ok: false, status: 0 });
		expect(first.ok ? '' : first.detail).toMatch(/^NOT CONFIRMED/);
		const [one, two] = sent.map((req) => req.headers.get('idempotency-key'));
		expect(one).toBeTruthy();
		expect(two).toBe(one);
	});
});
