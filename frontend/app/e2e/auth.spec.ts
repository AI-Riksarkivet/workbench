import { expect, test, type BrowserContext, type Page } from '@playwright/test';
import { EDGE } from './ports';
import { grantsOf, lakehouseOf, person, signIn } from './session';
import type { MockIdentity } from './mock-oidc';
import {
	APP_CLIENT,
	LAKEHOUSE_AUDIENCE,
	SERVICE_CLIENT,
	audienceScope,
	type Shape,
} from './providers';

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
const EXPECTED: Record<Shape, { scheme: RegExp; read: string; service: string }> = {
	keycloak: { scheme: /^DPoP /, read: 'exchanged', service: `service-account-${SERVICE_CLIENT}` },
	dex: { scheme: /^Bearer /, read: 'passed-on', service: SERVICE_CLIENT },
};

async function at(
	shape: Shape,
	page: Page,
	context: BrowserContext,
	name: string,
): Promise<MockIdentity> {
	if (shape === 'dex')
		await page.route('**/config.json', (route) => route.fulfill({ json: DEX_CONFIG }));
	const who = person(name);
	await signIn(context, who);
	return who;
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

for (const shape of ['keycloak', 'dex'] as const) {
	test.describe(`at a ${shape}-shaped provider`, () => {
		test('a person signs in, reads as themselves and writes through the annotator as the workbench', async ({
			page,
			context,
		}) => {
			const who = await at(shape, page, context, 'Annotator');
			const listed = page.waitForRequest((req) => req.url().endsWith('/api/viewer/api/datasets'));

			await page.goto('/datasets');
			await expect(page.getByRole('link', { name: /acme\$letters/ })).toBeVisible();
			const headers = await (await listed).allHeaders();
			expect(headers.authorization).toMatch(EXPECTED[shape].scheme);
			expect('dpop' in headers).toBe(shape === 'keycloak');

			await page.goto('/projects/pages/tasks/pages-t1?project=pages');
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
			expect(calls).toContainEqual({
				as: 'service',
				token: 'own',
				principal: EXPECTED[shape].service,
				admitted: true,
			});
			expect(calls.filter((call) => !call.admitted)).toEqual([]);
		});

		test('a session renews in place when its token ends, and sign-out ends it', async ({
			page,
			context,
		}) => {
			const who = await at(shape, page, context, 'Ada');
			await context.addCookies([{ name: 'mock_oidc_lifetime', value: '6', url: EDGE }]);

			await page.goto('/datasets');
			await expect(page.getByRole('link', { name: /acme\$letters/ })).toBeVisible();
			await expect.poll(() => grantsOf(page, who), { timeout: 15_000 }).toContain('refresh_token');
			await page.getByRole('link', { name: /acme\$letters/ }).click();
			await expect(page.getByRole('region', { name: 'Player' })).toBeVisible();
			expect(
				(await grantsOf(page, who)).filter((grant) => grant === 'authorization_code'),
			).toHaveLength(1);
			// The tokens live in memory alone: neither storage holds a token, a refresh token or a signed-in user.
			const stored = await page.evaluate(() =>
				[localStorage, sessionStorage].flatMap((store) =>
					Object.keys(store).map((key) => `${key}=${store.getItem(key) ?? ''}`),
				),
			);
			expect(stored.filter((entry) => /refresh\.|dpop-e2e\.|oidc\.user:/.test(entry))).toEqual([]);

			await page.getByRole('button', { name: 'Account' }).click();
			await page.getByRole('menuitem', { name: 'Sign out' }).click();
			// Keycloak-shaped, the issuer's session ends at its end-session endpoint; Dex-shaped, there is none, and
			// whoever comes next must authenticate again.
			await expect(page.getByRole('heading', { level: 1 })).toHaveText(
				shape === 'keycloak' ? 'Mock IdP signed out' : 'Mock IdP login',
			);
		});
	});
}
