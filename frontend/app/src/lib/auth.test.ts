import { afterAll, expect, it, vi } from 'vitest';

/** A Storage: `onRead` runs after each read of a key, as another tab writing just after this one read does. */
function storage(): Storage & { onRead: (key: string) => void } {
	const kept = new Map<string, string>();
	const store = {
		onRead: (_key: string): void => undefined,
		getItem: (key: string) => {
			const value = kept.get(key) ?? null;
			store.onRead(key);
			return value;
		},
		setItem: (key: string, value: string) => void kept.set(key, value),
		removeItem: (key: string) => void kept.delete(key),
	};
	return store as unknown as Storage & { onRead: (key: string) => void };
}

// The page the app runs in: an event target (the browser's `pageshow` and `storage`), its storages and its origin.
const page = Object.assign(new EventTarget(), {
	sessionStorage: storage(),
	localStorage: storage(),
});
vi.stubGlobal('window', page);
vi.stubGlobal('sessionStorage', page.sessionStorage);
vi.stubGlobal('localStorage', page.localStorage);

/** The sign-out record (`auth.svelte.ts`): the last sign-out's id, and the one the last sign-in answered. */
const SIGNED_OUT = 'workbench.signed-out';
const ANSWERED = 'workbench.signed-in-after';
// A page restored with nobody signed in loads again (`restored` in auth.svelte.ts), which here does nothing.
vi.stubGlobal('location', { origin: 'https://wb.example', reload: () => undefined });

/** The browser coming back to this page by Back, restored from the back/forward cache. */
const restore = (): void => {
	page.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: true }));
};

// The provider's client library as oidc-client-ts 3.5.0 behaves: a redirect leaves the page, and its promise settles
// only when the page is shown again (`RedirectNavigator.navigate` resolves on `pageshow`), or rejects when the
// navigation cannot start. `failNext` makes the next one fail, as an issuer that cannot be reached does.
// `signinRedirectCallback` (the code exchange) settles when the spec answers it (`library.answer`).
const library = vi.hoisted(() => ({
	redirects: [] as unknown[],
	failNext: false,
	answer: (_user: unknown): void => undefined,
}));
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
		signinRedirectCallback(): Promise<unknown> {
			return new Promise((resolve) => (library.answer = resolve));
		}
		async removeUser(): Promise<void> {}
	},
	ErrorResponse: class extends Error {},
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

/** The tokens the issuer answers a sign-in with, as oidc-client-ts holds them, for a sign-in sent when the last
 *  sign-out's id was `after`. */
const answered = (after: string) => ({
	state: { returnTo: '/projects/p1', after },
	profile: { sub: 'ada', name: 'Ada' },
	expires_at: 0,
});

it('a sign-in sent before a sign-out that another tab has since answered is not held, and the person is asked', async () => {
	const { auth, completeLogin } = await import('./auth.svelte.ts');
	restore();
	await settle();
	const completing = completeLogin();
	await settle();

	// While this tab is at the issuer, the person signs out in another tab, and someone signs in in a third: neither
	// is an event this tab sees.
	page.localStorage.setItem(SIGNED_OUT, 'signed-out-in-b');
	page.localStorage.setItem(ANSWERED, 'signed-out-in-b');
	library.answer(answered(''));
	await settle();
	const asked = library.redirects.at(-1);
	restore();

	expect([await completing, auth.tokensHeld, auth.session, asked]).toEqual([
		null,
		false,
		null,
		expect.objectContaining({ prompt: 'login' }),
	]);
});

it('a sign-in records the sign-out it answered and never removes one, not even a later one another tab wrote', async () => {
	const { auth, completeLogin } = await import('./auth.svelte.ts');
	page.localStorage.setItem(SIGNED_OUT, 'the-sign-out-it-answered');
	page.localStorage.setItem(ANSWERED, '');
	const completing = completeLogin();
	await settle();
	// Another tab signs out again just after this one read the record.
	page.localStorage.onRead = (key) => {
		if (key !== SIGNED_OUT) return;
		page.localStorage.onRead = () => undefined;
		page.localStorage.setItem(SIGNED_OUT, 'a-later-sign-out');
	};

	library.answer(answered('the-sign-out-it-answered'));

	expect([
		await completing,
		auth.tokensHeld,
		page.localStorage.getItem(SIGNED_OUT),
		page.localStorage.getItem(ANSWERED),
	]).toEqual(['/projects/p1', true, 'a-later-sign-out', 'the-sign-out-it-answered']);
});
