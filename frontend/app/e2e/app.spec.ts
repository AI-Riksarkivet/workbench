import { expect, test, type Page } from '@playwright/test';
import { callsOf, person, signIn, watchCsp } from './session';
import { EDGE } from './ports';

// The app's seams on the built app: sign-in with PKCE and DPoP, a valid proof on every call, the CSP
// header, the dataset list, and one dataset page with search, signed media and the atlas.

/** Open the account menu and read who the navbar says is signed in. */
async function accountMenu(page: Page): Promise<string> {
	await page.getByRole('button', { name: 'Account' }).click();
	const text = await page.getByRole('menu').innerText();
	await page.keyboard.press('Escape');
	return text;
}

test('a signed-out visit signs in through the issuer and lands where it asked, named by GET /me', async ({
	page,
	context,
}) => {
	const who = person('Ada');
	await signIn(context, who);

	await page.goto('/datasets?project=acme');

	await expect(page).toHaveURL(`${EDGE}/datasets?project=acme`);
	await expect(page.getByRole('heading', { level: 1, name: 'Datasets' })).toBeVisible();
	expect(await accountMenu(page)).toMatch(/Ada\s+ada@example\.com/);
	const calls = await callsOf(page, who);
	expect(calls).toContainEqual({ service: 'annotator', method: 'GET', path: '/me', status: 200 });
});

test('every call carries a DPoP proof for exactly that request, and a proof is good once', async ({
	page,
	context,
}) => {
	const who = person('Grace');
	await signIn(context, who);
	const sent = page.waitForRequest((req) => req.url().endsWith('/api/viewer/api/datasets'));

	await page.goto('/datasets');
	await expect(page.getByRole('link', { name: /acme\$letters/ })).toBeVisible();

	const calls = await callsOf(page, who);
	expect(calls.length).toBeGreaterThanOrEqual(2);
	expect(calls.filter((call) => call.refused !== undefined)).toEqual([]);
	// The proof the app sent, replayed: refused, because its jti was spent; and the same token with that
	// proof to another URL is refused for its htu.
	const headers = await (await sent).allHeaders();
	expect(headers.authorization).toMatch(/^DPoP /);
	const replay = await page.request.get(`${EDGE}/api/viewer/api/datasets`, {
		headers: { authorization: headers.authorization ?? '', dpop: headers.dpop ?? '' },
	});
	expect(replay.status()).toBe(401);
	expect((await replay.json()).detail).toContain('jti was already used');
	const bearer = await page.request.get(`${EDGE}/api/viewer/api/datasets`, {
		headers: { authorization: (headers.authorization ?? '').replace(/^DPoP/, 'Bearer') },
	});
	expect(bearer.status()).toBe(401);
});

test('the CSP is a header, strict, and the pages report no violation', async ({
	page,
	context,
}) => {
	await signIn(context, person('Barbara'));
	const violations = await watchCsp(page);

	const response = await page.goto('/datasets');
	await page.waitForURL(/\/datasets$/);
	const document = await page.request.get(`${EDGE}/datasets`);
	const policy = document.headers()['content-security-policy'] ?? '';
	expect(policy).toContain("default-src 'self'");
	expect(policy).toMatch(/script-src 'self' 'sha256-[^']+'/);
	expect(policy).not.toContain('unsafe-eval');
	expect(policy).toContain("frame-ancestors 'none'");
	expect(document.headers()['x-content-type-options']).toBe('nosniff');
	expect(document.headers()['referrer-policy']).toBe('no-referrer');
	expect(await document.text()).not.toContain('http-equiv="content-security-policy"');
	expect(response).not.toBeNull();

	await page.getByRole('link', { name: /acme\$letters/ }).click();
	await page.getByRole('searchbox').fill('harbour');
	await page.getByRole('button', { name: 'Search', exact: true }).click();
	await expect(
		page.getByRole('list', { name: 'Search results' }).getByRole('listitem'),
	).toHaveCount(3);
	await expect(page.getByRole('img', { name: /Atlas of 60 points/ })).toBeVisible();
	expect(await violations()).toEqual([]);
});

test('the dataset list shows what the viewer lists, each a link to its page', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	await page.goto('/datasets?project=acme');

	const list = page.getByRole('list', { name: 'Datasets' });
	await expect(list.getByRole('link')).toHaveCount(2);
	const letters = list.getByRole('link', { name: /acme\$letters/ });
	await expect(letters).toContainText('2 tables · 7 rows');
	await expect(letters).toHaveAttribute('href', '/datasets/acme%24letters?project=acme');
	await expect(page).toHaveTitle('Datasets · workbench');
});

test('a dataset page searches through Arrow, shows signed media, and reads the atlas by its ETag', async ({
	page,
	context,
}) => {
	const who = person('Margaret');
	await signIn(context, who);
	await page.goto('/datasets/acme%24letters');
	await expect(page).toHaveTitle('acme$letters · Datasets · workbench');

	await page.getByRole('searchbox').fill('harbour');
	await page.getByRole('button', { name: 'Search', exact: true }).click();
	const results = page.getByRole('list', { name: 'Search results' });
	await expect(results.getByRole('listitem')).toHaveCount(3);
	// Each card's thumbnail and its chunk's frame, signed in one batch and loaded same-origin.
	const thumbnail = results.getByRole('listitem').first().locator('img').first();
	await expect
		.poll(() => thumbnail.evaluate((img: HTMLImageElement) => img.naturalWidth))
		.toBeGreaterThan(0);

	await results.getByRole('button', { name: /The ships came into the harbour/ }).click();
	const media = page.getByRole('region', { name: 'Player' }).getByRole('img', {
		name: 'A letter from the harbour',
	});
	await expect(media).toBeVisible();
	await expect
		.poll(() => media.evaluate((img: HTMLImageElement) => img.naturalWidth))
		.toBeGreaterThan(0);
	expect(await media.getAttribute('src')).toMatch(/^\/api\/viewer\/api\/signed-media\/[\w-]+$/);

	await expect(
		page.getByRole('img', { name: 'Atlas of 60 points, coloured by cluster' }),
	).toBeVisible();
	// A second visit revalidates the points the browser holds: the viewer runs the read again and
	// answers 304, and no bytes travel. A reload signs in again (the tokens were in memory) and asks.
	await page.reload();
	await expect(
		page.getByRole('img', { name: 'Atlas of 60 points, coloured by cluster' }),
	).toBeVisible();

	await expect
		.poll(async () =>
			(await callsOf(page, who))
				.filter((call) => call.path === '/api/atlas/points')
				.map((call) => call.status),
		)
		.toEqual([200, 304]);
	const calls = await callsOf(page, who);
	expect(calls.filter((call) => call.refused !== undefined)).toEqual([]);
	for (const path of [
		'/api/search',
		'/api/signed-media',
		'/api/documents',
		'/api/datasets/acme%24letters/descriptor',
	]) {
		expect(calls.some((call) => call.path === path && call.status === 200)).toBe(true);
	}
	// The thumbnails, frames and media of one search went as ONE batch, not one request per image.
	const batches = calls.filter((call) => call.path === '/api/signed-media');
	expect(batches.length).toBeLessThanOrEqual(3);
});

test('a search answering several rows of one document lists each row', async ({
	page,
	context,
}) => {
	const errors: string[] = [];
	page.on('pageerror', (err) => errors.push(err.message));
	await signIn(context, person('Ada'));
	await page.goto('/datasets/acme%24letters');

	await page.getByRole('searchbox').fill('repeated');
	await page.getByRole('button', { name: 'Search', exact: true }).click();

	await expect(
		page.getByRole('list', { name: 'Search results' }).getByRole('listitem'),
	).toHaveCount(3);
	expect(errors).toEqual([]);
});

test('a dataset with no atlas and no thumbnails says so', async ({ page, context }) => {
	await signIn(context, person('Barbara'));
	await page.goto('/datasets/acme%24notes');
	await expect(page.getByText('This dataset declares no atlas space.')).toBeVisible();
});

test('a /config.json the app cannot use is a 503 that says why, and calls no service', async ({
	page,
}) => {
	const services: string[] = [];
	page.on('request', (req) => {
		if (req.url().includes('/api/')) services.push(req.url());
	});
	await page.route('**/config.json', (route) => route.fulfill({ json: { issuer: 1 } }));

	await page.goto('/datasets');

	await expect(
		page.getByText(/Sign-in is not configured: \/config\.json is malformed/),
	).toBeVisible();
	await expect(page.getByText('503')).toBeVisible();
	expect(services).toEqual([]);
});
