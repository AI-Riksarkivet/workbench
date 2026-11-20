import { expect, test, type BrowserContext, type Locator, type Page } from '@playwright/test';
import { EDGE } from './ports';
import { grantsOf, lakehouseOf, person, signIn } from './session';
import type { MockIdentity } from './mock-oidc';
import { APP_CLIENT, LAKEHOUSE_AUDIENCE, audienceScope, type Shape } from './providers';

// Sign-in at whatever OIDC provider the deployment runs, in the two shapes the workbench meets (`providers.ts`), on
// the built app: the provider's discovery decides DPoP, and the provider decides how a read reaches the lakehouse.
//
// - Keycloak-shaped (`/oidc`, the suite's default `/config.json`): DPoP-bound tokens, and the services exchange the
//   person's token for the lakehouse's audience.
// - Dex-shaped (`/oidc-dex`): no DPoP and no exchange of its own tokens, so the sign-in asks for the lakehouse's
//   audience too (Dex's cross-client scope), the person's bearer is passed on, and `federated:id` names the person as
//   the lakehouse names them; `offline_access` gives the refresh token a session is renewed with.
//
// The labeling canvas draws with WebGPU, so these run in the `webgpu` project (`playwright.config.ts`).

const SERVICES = { viewer: '/api/viewer', search: '/api/search', annotator: '/api/annotator' };

/** The `/config.json` a Dex-shaped deployment renders (`chart/`'s `app.oidc`, `deploy/values-dev.yaml`). */
const DEX_CONFIG = {
	issuer: '/oidc-dex',
	clientId: APP_CLIENT,
	scopes: [
		'openid',
		'profile',
		'email',
		'offline_access',
		'federated:id',
		audienceScope(LAKEHOUSE_AUDIENCE),
	],
	services: SERVICES,
	canvas: { autosaveIdleSeconds: 3600 },
};

/** How a request presents the token at each shape, and how the lakehouse is reached. */
const EXPECTED: Record<Shape, { scheme: RegExp; read: string }> = {
	keycloak: { scheme: /^DPoP /, read: 'exchanged' },
	dex: { scheme: /^Bearer /, read: 'passed-on' },
};

/**
 * The issuers the suite signs in at, as each real one keeps a login session or not (`mock-oidc.ts`):
 *
 * - `keycloak`: a session, `prompt=none` honoured, an end-session endpoint.
 * - `dex`: Dex v2.46.1 as the dev estate runs it, without `DEX_SESSIONS_ENABLED`: no session, so every sign-in shows its
 *   form and there is no end-session endpoint; the deployment turns silent sign-in off (`deploy/values-dev.yaml`).
 * - `dex-sessions`: Dex with `DEX_SESSIONS_ENABLED`: a session, `prompt=none` honoured, an end-session endpoint.
 */
type Issuer = 'keycloak' | 'dex' | 'dex-sessions';
const SHAPE_OF: Record<Issuer, Shape> = { keycloak: 'keycloak', dex: 'dex', 'dex-sessions': 'dex' };

async function at(
	issuer: Issuer,
	page: Page,
	context: BrowserContext,
	name: string,
): Promise<MockIdentity> {
	if (issuer !== 'keycloak') {
		const json = { ...DEX_CONFIG, silentSignIn: issuer === 'dex-sessions' };
		await page.route('**/config.json', (route) => route.fulfill({ json }));
	}
	if (issuer === 'dex-sessions')
		await context.addCookies([{ name: 'mock_oidc_dex_sessions', value: 'on', url: EDGE }]);
	const who = person(name);
	await signIn(context, who);
	return who;
}

/** Wait until `arrived` shows, signing in at each form the issuer shows on the way (as the person does, with the
 *  password `signIn` set) and granting its approval screen; answers how many login forms the person met. */
async function throughForms(page: Page, arrived: Locator, timeout?: number): Promise<number> {
	const form = page.getByRole('button', { name: /^(Sign in|Grant access)$/ });
	let forms = 0;
	for (;;) {
		await expect(form.or(arrived).first()).toBeVisible({ timeout });
		if (await arrived.isVisible()) return forms;
		if ((await form.textContent()) === 'Sign in') forms++;
		await Promise.all([page.waitForEvent('framenavigated'), form.click()]);
	}
}

/** The datasets page, signed in. */
const signedIn = (page: Page): Locator => page.getByRole('link', { name: /acme\$letters/ });

/** Arrive at the signed-in page: at an issuer that keeps no session, through its form. */
async function arrive(issuer: Issuer, page: Page): Promise<void> {
	if (issuer === 'dex') await throughForms(page, signedIn(page));
	else await expect(signedIn(page)).toBeVisible();
}

/** An address with a query and a fragment, which a sign-in round trip must come back to whole. */
const ADDRESS = '/datasets?q=letters#list';

/** The `prompt` of every authorization request the app sends, in order (`-` for none); the issuer's own login form
 *  sending one on is the person's, not the app's. */
function prompts(page: Page): string[] {
	const asked: string[] = [];
	page.on('request', (req) => {
		const url = new URL(req.url());
		if (/^\/oidc(-dex)?\/auth$/.test(url.pathname) && !url.searchParams.has('mock_signed_in'))
			asked.push(url.searchParams.get('prompt') ?? '-');
	});
	return asked;
}

/** Every page the browser showed from the issuer (a redirect it answered shows none). */
function issuerPages(page: Page): string[] {
	const shown: string[] = [];
	page.on('framenavigated', (frame) => {
		if (frame === page.mainFrame() && /^\/oidc(-dex)?\//.test(new URL(frame.url()).pathname))
			shown.push(frame.url());
	});
	return shown;
}

/** The person's issuer session ends there (it expired, or they signed out elsewhere). */
async function issuerSessionEnds(context: BrowserContext): Promise<void> {
	await context.clearCookies({ name: 'mock_oidc_identity' });
}

/** A box dragged across the middle of the labeling canvas, once it says it takes input. */
async function drawABox(page: Page): Promise<void> {
	await expect(page.getByTestId('canvas-region')).toHaveAttribute('data-ready', 'true');
	await page.keyboard.press('b');
	const box = await page.getByTestId('pixi-canvas').boundingBox();
	if (!box) throw new Error('the canvas has no box: the engine never rendered');
	await page.mouse.move(box.x + box.width * 0.55, box.y + box.height * 0.2);
	await page.mouse.down();
	await page.mouse.move(box.x + box.width * 0.75, box.y + box.height * 0.4, { steps: 4 });
	await page.mouse.up();
	// The class picker opens on the drawn box: the task's first class.
	await page.keyboard.press('1');
	await expect(page.getByTestId('class-picker')).toBeHidden();
}

test('a discovery document naming another issuer is refused before any of its endpoints is used', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	const issuerCalls: string[] = [];
	page.on('request', (req) => {
		if (new URL(req.url()).pathname.startsWith('/oidc/')) issuerCalls.push(req.url());
	});
	await page.route('**/oidc/.well-known/openid-configuration', async (route) => {
		const real = await (await route.fetch()).json();
		await route.fulfill({ json: { ...real, issuer: 'http://elsewhere.example/oidc' } });
	});

	await page.goto('/datasets');

	await expect(page.getByText(/names the issuer http:\/\/elsewhere\.example\/oidc/)).toBeVisible();
	expect(issuerCalls.filter((url) => !url.includes('/.well-known/'))).toEqual([]);
});

test('an app whose client the provider does not know says so when the person comes back from the provider', async ({
	page,
	context,
}) => {
	await page.route('**/config.json', (route) =>
		route.fulfill({ json: { ...DEX_CONFIG, clientId: 'a-client-nobody-registered' } }),
	);
	await signIn(context, person('Ada'));
	// The provider answers a client it does not know with an error of its own and never sends the person back.
	await page.goto('/datasets');
	await expect(page).toHaveURL(/\/oidc-dex\/auth\?/);

	await page.goBack();

	await expect(
		page.getByText(/does not know this app's client "a-client-nobody-registered"/),
	).toBeVisible();
});

test('a deployment that requires bound tokens refuses a provider that offers no DPoP', async ({
	page,
	context,
}) => {
	await page.route('**/config.json', (route) =>
		route.fulfill({ json: { ...DEX_CONFIG, dpop: 'required' } }),
	);
	await signIn(context, person('Ada'));

	await page.goto('/datasets');

	await expect(page.getByText(/requires DPoP-bound tokens/)).toBeVisible();
});

for (const issuer of ['keycloak', 'dex', 'dex-sessions'] as const) {
	const shape = SHAPE_OF[issuer];
	test.describe(`at a ${issuer} provider`, () => {
		if (issuer !== 'dex-sessions')
			test('a person signs in, reads as themselves and labels through the annotator, which writes nothing to the lakehouse', async ({
				page,
				context,
			}) => {
				const who = await at(issuer, page, context, 'Annotator');
				const listed = page.waitForRequest((req) => req.url().endsWith('/api/viewer/api/datasets'));

				await page.goto('/datasets');
				await arrive(issuer, page);
				const headers = await (await listed).allHeaders();
				expect(headers.authorization).toMatch(EXPECTED[shape].scheme);
				expect('dpop' in headers).toBe(shape === 'keycloak');

				await page.goto('/projects/pages/tasks/pages-t1?project=pages');
				// The labeling canvas's page draws on the CPU here (`playwright.config.ts`), and a sign-in at the form comes
				// first: measured 2026-11-20 on a loaded host (load average 25-48), the two together passed the 10 s an
				// assertion waits, so this one waits 20 s.
				if (issuer === 'dex') await throughForms(page, page.getByTestId('canvas-region'), 20_000);
				await drawABox(page);
				await page.getByRole('button', { name: 'Save', exact: true }).click();
				await expect(page.getByTestId('canvas-notice')).toHaveText('Annotations saved');

				const calls = await lakehouseOf(page, who);
				expect(calls).toContainEqual({
					as: 'person',
					token: EXPECTED[shape].read,
					principal: who.sub,
					admitted: true,
				});
				// The save is the annotator's own until a publish: every lakehouse call was the person's read.
				expect(calls.filter((call) => call.as !== 'person' || !call.admitted)).toEqual([]);
			});

		test('a session renews in place when its token ends, and sign-out ends it so a reload asks the person', async ({
			page,
			context,
		}) => {
			const who = await at(issuer, page, context, 'Ada');
			await context.addCookies([{ name: 'mock_oidc_lifetime', value: '6', url: EDGE }]);

			await page.goto('/datasets');
			await arrive(issuer, page);
			await expect.poll(() => grantsOf(page, who), { timeout: 15_000 }).toContain('refresh_token');
			await page.getByRole('link', { name: /acme\$letters/ }).click();
			await expect(page.getByRole('region', { name: 'Player' })).toBeVisible();
			expect(
				(await grantsOf(page, who)).filter((grant) => grant === 'authorization_code'),
			).toHaveLength(1);
			const asked = prompts(page);
			const ends: URLSearchParams[] = [];
			page.on('request', (req) => {
				const url = new URL(req.url());
				if (url.pathname.endsWith('/logout')) ends.push(url.searchParams);
			});

			await page.getByRole('button', { name: 'Account' }).click();
			await page.getByRole('menuitem', { name: 'Sign out' }).click();
			// Where the issuer keeps a session, it ends at its end-session endpoint, which names it by the id token and
			// sends the browser back to the app; Dex without its sessions has none. Either way the next sign-in authenticates.
			await expect(page.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');
			expect(
				ends.map((ended) => [ended.has('id_token_hint'), ended.get('post_logout_redirect_uri')]),
			).toEqual(issuer === 'dex' ? [] : [[true, `${EDGE}/`]]);
			// Sign out, then reload the app: the sign-in page, never a silent sign-in from a session the issuer kept.
			await page.goto(ADDRESS);
			await expect(page.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');
			expect(asked.at(-1)).toBe('login');
			expect(asked).not.toContain('none');
			// The person signs in again: from then on a reload is as it was before the sign-out, silent where the
			// deployment allows it, and never forced to authenticate again.
			await signIn(context, who);
			await throughForms(page, signedIn(page));
			const again = prompts(page);
			await page.reload();
			await arrive(issuer, page);
			expect(again).toEqual([issuer === 'dex' ? '-' : 'none']);
		});

		if (issuer !== 'dex')
			test('a reload signs the person in again from the issuer session, without a form, at the same address, and stores no token', async ({
				page,
				context,
			}) => {
				const who = await at(issuer, page, context, 'Ada');
				const tokens: string[] = [];
				page.on('response', async (res) => {
					if (!res.url().endsWith('/token') || res.request().method() !== 'POST') return;
					const body = (await res.json().catch(() => null)) as Record<string, unknown> | null;
					for (const kind of ['access_token', 'refresh_token', 'id_token'])
						if (typeof body?.[kind] === 'string') tokens.push(body[kind]);
				});
				await page.goto(ADDRESS);
				await expect(signedIn(page)).toBeVisible();
				const asked = prompts(page);
				const shown = issuerPages(page);

				await page.reload();

				await expect(signedIn(page)).toBeVisible();
				expect(new URL(page.url()).href).toBe(`${EDGE}${ADDRESS}`);
				expect([asked, shown]).toEqual([['none'], []]);
				expect(
					(await grantsOf(page, who)).filter((grant) => grant === 'authorization_code'),
				).toHaveLength(2);
				// The tokens live in memory alone: no storage and no cookie the page can read holds one.
				await expect.poll(() => tokens.length).toBe(6);
				const readable = await page.evaluate(() => [
					document.cookie,
					...[localStorage, sessionStorage].flatMap((store) =>
						Object.keys(store).map((key) => `${key}=${store.getItem(key) ?? ''}`),
					),
				]);
				expect(readable.filter((entry) => tokens.some((token) => entry.includes(token)))).toEqual(
					[],
				);
			});
	});
}

test('with no session at the issuer, a reload shows its sign-in page once, and signing in there returns to the same address', async ({
	page,
	context,
}) => {
	const who = person('Ada');
	await signIn(context, who);
	await page.goto(ADDRESS);
	await expect(signedIn(page)).toBeVisible();
	await issuerSessionEnds(context);
	const asked = prompts(page);

	await page.reload();

	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');
	await signIn(context, who);
	await page.getByRole('button', { name: 'Sign in' }).click();
	await expect(signedIn(page)).toBeVisible();
	expect(new URL(page.url()).href).toBe(`${EDGE}${ADDRESS}`);
	expect(asked).toEqual(['none', '-']);
});

// Dex v2.46.1 without its sessions does not act on `prompt=none` at login: it shows its form, and after it decides
// consent. With `skipApprovalScreen: true` (the dev estate) it then issues the code; with its default, consent is never
// stored without sessions, so a request that carried `prompt=none` is refused `interaction_required` after the form,
// and the app asks again interactively: the person meets the form twice. Silent sign-in off, the app never sends
// `prompt=none`, and every reload costs one form.
for (const approval of ['skipApprovalScreen', 'approvalScreen'] as const)
	for (const silentSignIn of [true, false])
		test(`at Dex without its sessions (${approval}), silent sign-in ${silentSignIn ? 'on' : 'off'}: a reload signs in through ${approval === 'approvalScreen' && silentSignIn ? 'two forms' : 'one form'}, back at the same address`, async ({
			page,
			context,
		}) => {
			const json = { ...DEX_CONFIG, silentSignIn };
			await page.route('**/config.json', (route) => route.fulfill({ json }));
			if (approval === 'approvalScreen')
				await context.addCookies([{ name: 'mock_oidc_dex_approval', value: 'screen', url: EDGE }]);
			await signIn(context, person('Ada'));
			await page.goto(ADDRESS);
			await throughForms(page, signedIn(page));
			const asked = prompts(page);

			await page.reload();
			const forms = await throughForms(page, signedIn(page));

			expect(new URL(page.url()).href).toBe(`${EDGE}${ADDRESS}`);
			expect([forms, asked]).toEqual(
				silentSignIn
					? approval === 'approvalScreen'
						? [2, ['none', '-']]
						: [1, ['none']]
					: [1, ['-']],
			);
		});

test('a provider that answers prompt=none with its own error page: Back restores the app from the back/forward cache, and it asks the person, never silently again', async ({
	playwright,
}) => {
	// Playwright starts Chromium with the back/forward cache off; this browser runs with it, so Back restores the page as
	// a person's browser does.
	const browser = await playwright.chromium.launch({
		ignoreDefaultArgs: ['--disable-back-forward-cache'],
	});
	await using _closed = { [Symbol.asyncDispose]: () => browser.close() };
	const context = await browser.newContext({ baseURL: EDGE });
	const page = await context.newPage();
	await context.addCookies([{ name: 'mock_oidc_prompt_none', value: 'error-page', url: EDGE }]);
	await page.addInitScript(() => {
		addEventListener('pageshow', (event: PageTransitionEvent) => {
			if (event.persisted) sessionStorage.setItem('e2e.restored', 'yes');
		});
	});
	const asked = prompts(page);

	await page.goto(ADDRESS);
	// The provider's own error page, which never returns to the app: the person comes back by Back.
	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Mock IdP error');
	// A restored page fires `pageshow`, not `load`, so Back is waited for only until it commits.
	await page.goBack({ waitUntil: 'commit' });
	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');
	await signIn(context, person('Ada'));
	await page.getByRole('button', { name: 'Sign in' }).click();

	await expect(signedIn(page)).toBeVisible();
	expect(new URL(page.url()).href).toBe(`${EDGE}${ADDRESS}`);
	expect([asked, await page.evaluate(() => sessionStorage.getItem('e2e.restored'))]).toEqual([
		['none', '-'],
		'yes',
	]);
});

test('the issuer ends its session only on a sign-out it accepts', async ({ page, context }) => {
	const who = person('Ada');
	await signIn(context, who);
	const hint = (iss: string): string =>
		[{ alg: 'none' }, { iss, sub: who.sub }, null]
			.map((part) => (part ? Buffer.from(JSON.stringify(part)).toString('base64url') : ''))
			.join('.');
	const session = async (): Promise<boolean> =>
		(await context.cookies(EDGE)).some((cookie) => cookie.name === 'mock_oidc_identity');
	const refused: boolean[] = [];

	for (const query of <Record<string, string>[]>[
		{},
		{ id_token_hint: hint('http://elsewhere.example/oidc') },
		{ id_token_hint: hint(`${EDGE}/oidc`), post_logout_redirect_uri: 'http://elsewhere.example/' },
	]) {
		await page.goto(`/oidc/logout?${new URLSearchParams(query)}`);
		await expect(page.getByRole('heading', { level: 1 })).toHaveText('Mock IdP refused sign-out');
		refused.push(await session());
	}
	await page.goto(
		`/oidc/logout?${new URLSearchParams({ id_token_hint: hint(`${EDGE}/oidc`), post_logout_redirect_uri: `${EDGE}/` })}`,
	);

	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');
	expect([refused, await session()]).toEqual([[true, true, true], false]);
});

test('a sign-in another tab started before the person signed out is dropped, and that tab asks the person', async ({
	page,
	context,
}) => {
	// An issuer that keeps its session through the sign-out (no end-session endpoint), so only the app can refuse it.
	await context.addCookies([{ name: 'mock_oidc_no_end_session', value: '1', url: EDGE }]);
	await signIn(context, person('Ada'));
	await page.goto(ADDRESS);
	await expect(signedIn(page)).toBeVisible();
	const other = await context.newPage();
	let release = (): void => undefined;
	const held = new Promise<void>((resolve) => (release = resolve));
	await other.route(/\/oidc\/auth\?/, async (route) => {
		await held;
		await route.continue();
	});
	const asked = prompts(other);
	const leaving = other.waitForRequest((req) => new URL(req.url()).pathname === '/oidc/auth');
	await other.goto(ADDRESS);
	await leaving;

	await page.getByRole('button', { name: 'Account' }).click();
	await page.getByRole('menuitem', { name: 'Sign out' }).click();
	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');
	release();

	await expect(other.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');
	expect(asked).toEqual(['none', 'login']);
});

/** Another tab of the same browser, configured as `issuer`'s deployment is (`at`). */
async function tabAt(issuer: Issuer, context: BrowserContext): Promise<Page> {
	const tab = await context.newPage();
	if (issuer !== 'keycloak') {
		const json = { ...DEX_CONFIG, silentSignIn: issuer === 'dex-sessions' };
		await tab.route('**/config.json', (route) => route.fulfill({ json }));
	}
	return tab;
}

for (const issuer of ['keycloak', 'dex'] as const)
	test(`at a ${issuer} provider, a tab still signed in when another signs out drops its tokens and asks the person by the prompt, going nowhere on its own`, async ({
		page,
		context,
	}) => {
		await at(issuer, page, context, 'Ada');
		await page.goto(ADDRESS);
		await arrive(issuer, page);
		const other = await tabAt(issuer, context);
		const reads: (string | undefined)[] = [];
		other.on('request', (req) => {
			if (new URL(req.url()).pathname.startsWith('/api/viewer/'))
				reads.push(req.headers().authorization);
		});
		await other.goto(ADDRESS);
		await arrive(issuer, other);
		const asked = prompts(other);
		const before = reads.length;

		await page.getByRole('button', { name: 'Account' }).click();
		await page.getByRole('menuitem', { name: 'Sign out' }).click();
		await expect(page.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');

		const prompt = other.getByTestId('session-prompt');
		await expect(prompt).toContainText('You signed out in another tab');
		await expect(signedIn(other)).toBeVisible();
		expect([new URL(other.url()).href, asked]).toEqual([`${EDGE}${ADDRESS}`, []]);
		// A read the page sends from now on carries no token.
		await other.getByRole('link', { name: /acme\$letters/ }).click();
		await expect.poll(() => reads.length).toBeGreaterThan(before);
		expect(reads.slice(0, before).every((authorization) => authorization !== undefined)).toBe(true);
		expect(reads.slice(before)).toEqual(reads.slice(before).map(() => undefined));

		// A page that guards its leaving (the browser's "Leave site?"): the person answers Stay, and their next "Sign in
		// again" still leaves, rather than joining the sign-in their Stay cancelled.
		await other.evaluate(() =>
			addEventListener('beforeunload', (event) => {
				event.preventDefault();
			}),
		);
		const answers = ['dismiss', 'accept'] as const;
		const dialogs: string[] = [];
		other.on('dialog', async (dialog) => {
			const answer = answers[dialogs.length] ?? 'accept';
			dialogs.push(dialog.type());
			await dialog[answer]();
		});
		await prompt.getByRole('button', { name: 'Sign in again' }).click();
		await expect.poll(() => dialogs.length).toBe(1);
		await expect(prompt).toBeVisible();
		await prompt.getByRole('button', { name: 'Sign in again' }).click();

		await expect(other.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');
		expect([dialogs, asked.at(-1)]).toEqual([['beforeunload', 'beforeunload'], 'login']);
	});

// The other tab's token is renewed while the person signs out in this one: the renewal is held at the issuer until the
// sign-out has reached that tab. Answered, its fresh tokens must not be kept: a read the page starts after it goes
// unsigned. Refused, a read that was waiting on it must not fall back to the token from before the sign-out.
for (const refresh of ['answered', 'refused'] as const)
	test(`at a dex provider, a renewal ${refresh} after another tab signed out signs nothing in this one`, async ({
		page,
		context,
	}) => {
		await at('dex', page, context, 'Ada');
		await context.addCookies([{ name: 'mock_oidc_lifetime', value: '8', url: EDGE }]);
		await page.goto(ADDRESS);
		await arrive('dex', page);
		const other = await tabAt('dex', context);
		await other.goto(ADDRESS);
		await arrive('dex', other);
		const reads: (string | undefined)[] = [];
		other.on('request', (req) => {
			if (new URL(req.url()).pathname.startsWith('/api/')) reads.push(req.headers().authorization);
		});
		const renewals: string[] = [];
		let release = (): void => undefined;
		const released = new Promise<void>((resolve) => (release = resolve));
		let heldNow = (): void => undefined;
		const held = new Promise<void>((resolve) => (heldNow = resolve));
		await other.route('**/oidc-dex/token', async (route) => {
			if (!(route.request().postData() ?? '').includes('grant_type=refresh_token'))
				return route.continue();
			renewals.push(refresh);
			if (renewals.length > 1) return route.continue();
			heldNow();
			await released;
			if (refresh === 'refused')
				return route.fulfill({ status: 400, json: { error: 'invalid_grant' } });
			return route.continue();
		});
		// The session's own timer asks for the renewal when its token nears its end.
		await held;
		const dataset = other.getByRole('link', { name: /acme\$letters/ });
		// Refused: a read starts now and waits on the renewal: the signer marks the page's timeline when it does
		// (`SIGNER_WAITS` in auth.svelte.ts), and the sign-out is written only once it has.
		if (refresh === 'refused') {
			const waits = (): Promise<number> =>
				other.evaluate(
					() =>
						performance.getEntriesByName('workbench.signer-waits-for-renewal').at(-1)?.startTime ??
						-1,
				);
			const before = await waits();
			await dataset.click();
			await expect.poll(waits).toBeGreaterThan(before);
		}

		// The sign-out's mark, written as the first tab's sign-out writes it: the token from before has 2 s left
		// (`RENEW_MARGIN_SECONDS`), and the refused case needs it unexpired, so the sign-out's own round trip is not
		// waited for.
		await page.evaluate(() => localStorage.setItem('workbench.signed-out', crypto.randomUUID()));
		const prompt = other.getByTestId('session-prompt');
		await expect(prompt).toContainText('You signed out in another tab');
		const settled = other.waitForResponse(
			(res) =>
				res.url().endsWith('/oidc-dex/token') &&
				(res.request().postData() ?? '').includes('grant_type=refresh_token'),
		);
		const before = reads.length;
		release();
		await settled;
		// Answered: a read the page starts after the renewal settled.
		if (refresh === 'answered') await dataset.click();

		await expect.poll(() => reads.length).toBeGreaterThan(before);
		await expect(prompt).toBeVisible();
		expect([
			reads.slice(before).filter((authorization) => authorization !== undefined),
			renewals,
		]).toEqual([[], [refresh]]);
	});

test('a sign-out in another tab keeps the canvas’s unsaved edits on screen, and through signing in again', async ({
	page,
	context,
}) => {
	const who = person('Annotator');
	await signIn(context, who);
	const other = await context.newPage();
	const dialogs: string[] = [];
	other.on('dialog', async (dialog) => {
		dialogs.push(dialog.type());
		await dialog.dismiss();
	});
	await other.goto('/projects/pages/tasks/pages-t1?project=pages');
	await expect(other.getByTestId('canvas-region')).toHaveAttribute('data-ready', 'true');
	const shapes = other.getByTestId('annotation-list').getByRole('button');
	const drawn = (await shapes.count()) + 1;
	await drawABox(other);
	await expect(shapes).toHaveCount(drawn);
	await page.goto(ADDRESS);
	await expect(signedIn(page)).toBeVisible();

	await page.getByRole('button', { name: 'Account' }).click();
	await page.getByRole('menuitem', { name: 'Sign out' }).click();
	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');

	const prompt = other.getByTestId('session-prompt');
	await expect(prompt).toContainText('You signed out in another tab');
	await expect(shapes).toHaveCount(drawn);
	await prompt.getByRole('button', { name: 'Sign in again' }).click();
	await expect(other.getByRole('heading', { level: 1 })).toHaveText('Mock IdP login');
	await signIn(context, who);
	await other.getByRole('button', { name: 'Sign in' }).click();

	await expect(other).toHaveURL(/\/tasks\/pages-t1\?project=pages$/);
	await expect(other.getByTestId('canvas-notice')).toHaveText(
		'Your unsaved edits are back. Save to keep them.',
	);
	await expect(shapes).toHaveCount(drawn);
	expect(dialogs).toEqual([]);
});
