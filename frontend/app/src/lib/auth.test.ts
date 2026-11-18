import { afterAll, expect, it, vi } from 'vitest';

// The page the app runs in: an event target (the browser's `pageshow`), its storage and its origin.
const page = Object.assign(new EventTarget(), {
	sessionStorage: (() => {
		const kept = new Map<string, string>();
		return {
			getItem: (key: string) => kept.get(key) ?? null,
			setItem: (key: string, value: string) => void kept.set(key, value),
			removeItem: (key: string) => void kept.delete(key),
		};
	})(),
});
vi.stubGlobal('window', page);
vi.stubGlobal('sessionStorage', page.sessionStorage);
vi.stubGlobal('location', { origin: 'https://wb.example' });

/** The browser coming back to this page by Back, restored from the back/forward cache. */
const restore = (): void => {
	page.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: true }));
};

// The provider's client library as oidc-client-ts 3.5.0 behaves: a redirect leaves the page, and its promise settles
// only when the page is shown again (`RedirectNavigator.navigate` resolves on `pageshow`), or rejects when the
// navigation cannot start. `failNext` makes the next one fail, as an issuer that cannot be reached does.
const library = vi.hoisted(() => ({ redirects: [] as unknown[], failNext: false }));
vi.mock('oidc-client-ts', () => ({
	UserManager: class {
		settings = { client_id: 'workbench-app', redirect_uri: 'https://wb.example/auth/callback' };
		metadataService = { getTokenEndpoint: async () => 'https://id.example/token' };
		signinRedirect(args: unknown): Promise<void> {
			library.redirects.push(args);
			if (library.failNext) {
				library.failNext = false;
				return Promise.reject(new Error('the issuer could not be reached'));
			}
			return new Promise((resolve) =>
				window.addEventListener('pageshow', () => resolve(), { once: true }),
			);
		}
	},
	WebStorageStateStore: class {},
	InMemoryWebStorage: class {},
	User: class {},
}));

// Every call the sign-in makes to the provider's token endpoint (the probe of its client is one).
const tokenCalls: string[] = [];
vi.stubGlobal('fetch', async (url: string) => {
	tokenCalls.push(url);
	return Response.json({ error: 'invalid_grant' }, { status: 400 });
});

/** `/config.json` and the provider's discovery, as a deployment that signs people in answers them. */
const served: typeof fetch = async (input) =>
	String(input).endsWith('/config.json')
		? Response.json({
				issuer: 'https://id.example',
				clientId: 'workbench-app',
				scopes: ['openid'],
				services: { viewer: '/api/viewer', search: '/api/search', annotator: '/api/annotator' },
			})
		: Response.json({
				issuer: 'https://id.example',
				authorization_endpoint: 'https://id.example/auth',
				token_endpoint: 'https://id.example/token',
			});

afterAll(() => {
	vi.unstubAllGlobals();
});

/** Let every settled promise run its continuations. */
const settle = (): Promise<void> => new Promise((done) => setTimeout(done, 20));

it('sends a page to sign in once however many ask at once, again after Back, and again after a failure', async () => {
	const { initAuth, login } = await import('./auth.svelte.ts');
	await initAuth(served);

	// Two at once: one redirect, and nothing asked of the token endpoint.
	void login('/projects/p1');
	void login('/projects/p1/tasks/t1');
	await settle();
	const atOnce = [library.redirects.length, tokenCalls.length];
	// The person comes back by Back: two more, one redirect more.
	restore();
	await settle();
	void login('/projects/p1');
	void login('/projects/p1');
	await settle();
	const afterBack = library.redirects.length;
	// The issuer cannot be reached: the failed sign-in frees the next.
	restore();
	await settle();
	library.failNext = true;
	login('/a').catch(() => undefined);
	await settle();
	void login('/b');
	await settle();

	expect([atOnce, afterBack, library.redirects.length]).toEqual([[1, 0], 2, 4]);
});
