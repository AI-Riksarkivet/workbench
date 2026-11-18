import { expect, test, type Page } from '@playwright/test';
import { ACCENT_SURFACES, ACCENT_TINTS } from '../../packages/ui/src/lib/appearance/appearance.ts';
import { THEMES } from '../../packages/ui/src/lib/appearance/core.ts';
import { accentOnSurfaces, textContrast } from './contrast';
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
	await expect(page.getByText('WebGPU is required (no GPU adapter available)')).toBeVisible();
	expect(await violations()).toEqual([]);
});

test('the dataset list shows what the viewer lists, each a link to its page', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	await page.goto('/datasets?project=acme');

	const list = page.getByRole('list', { name: 'Datasets in The lakehouse' });
	await expect(list.getByRole('link')).toHaveCount(4);
	const letters = list.getByRole('link', { name: /acme\$letters/ });
	await expect(letters).toContainText('2 tables · 7 rows');
	await expect(letters).toHaveAttribute('href', '/datasets/lake~acme%24letters?project=acme');
	await expect(page).toHaveTitle('Datasets · workbench');
});

test('a dataset page searches through Arrow and shows signed media', async ({ page, context }) => {
	const who = person('Margaret');
	await signIn(context, who);
	await page.goto('/datasets/lake~acme%24letters');
	await expect(page).toHaveTitle('lake~acme$letters · Datasets · workbench');

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
	// The player names the document and the page it opened; the page's picture is the WebGPU project's to draw
	// (`player.spec.ts`).
	const player = page.getByRole('region', { name: 'Player' });
	await expect(player.getByRole('heading', { name: 'A letter from the harbour' })).toBeVisible();
	await expect(player.getByTestId('viewer-position')).toHaveText('Page 1 of 2');

	const calls = await callsOf(page, who);
	expect(calls.filter((call) => call.refused !== undefined)).toEqual([]);
	for (const path of [
		'/api/search',
		'/api/signed-media',
		'/api/documents',
		'/api/datasets/lake~acme%24letters/descriptor',
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
	await page.goto('/datasets/lake~acme%24letters');

	await page.getByRole('searchbox').fill('repeated');
	await page.getByRole('button', { name: 'Search', exact: true }).click();

	await expect(
		page.getByRole('list', { name: 'Search results' }).getByRole('listitem'),
	).toHaveCount(3);
	expect(errors).toEqual([]);
});

test('the sidebar starts as the collapsed rail, and opened it stays open across a reload', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets');
	const sidebar = page.locator('[data-slot="sidebar"][data-state]');
	await expect(sidebar).toHaveAttribute('data-state', 'collapsed');
	// Each rail icon keeps its name.
	await expect(
		page.getByRole('navigation', { name: 'Pages' }).getByRole('link', { name: 'Datasets' }),
	).toBeVisible();

	await page.getByRole('main').getByRole('button', { name: 'Toggle Sidebar' }).click();
	await expect(sidebar).toHaveAttribute('data-state', 'expanded');
	await page.reload();
	await expect(sidebar).toHaveAttribute('data-state', 'expanded');
});

test('a viewer that cannot be reached is told apart from a dataset that does not exist, and is tried again', async ({
	page,
	context,
}) => {
	await signIn(context, person('Barbara'));
	// The edge's answer for a service it cannot reach.
	await page.route('**/api/viewer/api/datasets/*/descriptor', (route) =>
		route.fulfill({
			status: 502,
			contentType: 'application/problem+json',
			json: {
				type: 'about:blank',
				title: 'Bad Gateway',
				status: 502,
				detail: 'the viewer service could not be reached',
			},
		}),
	);
	await page.goto('/datasets/lake~acme%24letters');
	await expect(page.getByRole('alert')).toHaveText(
		'The viewer could not be reached, so this dataset cannot be shown right now.',
	);
	await expect(page.getByText('that you can see')).toHaveCount(0);

	await page.unroute('**/api/viewer/api/datasets/*/descriptor');
	await page.getByRole('button', { name: 'Try again' }).click();
	await expect(page.getByRole('region', { name: 'Player' })).toBeVisible();
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

test('a theme and mode picked in the account menu are applied, kept, and on the page before the app runs', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets');
	const html = page.locator('html');
	// A first visit opens dark in the default theme.
	await expect(html).toHaveClass(/dark/);
	await expect(html).toHaveAttribute('data-theme', 'ink');

	await page.getByRole('button', { name: 'Account' }).click();
	await page.getByRole('menuitemradio', { name: 'Fjord' }).click();
	await page.getByRole('menuitemradio', { name: 'Light' }).click();
	await expect(page.getByRole('menuitemradio', { name: 'Light' })).toHaveAttribute(
		'aria-checked',
		'true',
	);
	await expect(html).toHaveAttribute('data-theme', 'fjord');
	await expect(html).not.toHaveClass(/dark/);
	await page.keyboard.press('Escape');

	// Kept across a reload, and applied by the boot script alone: with the app's code refused, the page still has it.
	await page.route('**/_app/**', (route) => route.abort());
	await page.reload();
	await expect(html).toHaveAttribute('data-theme', 'fjord');
	await expect(html).not.toHaveClass(/dark/);

	// A record that is not one the app writes sets nothing it does not name: an unknown theme is the default, a stored
	// token is ignored, and the record is written back clean.
	await page.evaluate(() =>
		localStorage.setItem(
			'workbench-appearance',
			JSON.stringify({ mode: 'dark', theme: '"><script>', vars: { '--background': 'red' } }),
		),
	);
	await page.reload();
	await expect(html).toHaveAttribute('data-theme', 'ink');
	await expect(html).toHaveClass(/dark/);
	expect(
		await html.evaluate((el) => getComputedStyle(el).getPropertyValue('--background')),
	).not.toBe('red');
	expect(await html.evaluate((el) => el.getAttribute('style') ?? '')).not.toContain('--background');
	expect(
		JSON.parse((await page.evaluate(() => localStorage.getItem('workbench-appearance'))) ?? '{}'),
	).toEqual({
		v: 2,
		theme: 'ink',
		mode: 'dark',
		accent: null,
	});
	await page.unroute('**/_app/**');
});

test('the system mode follows the system, and an accent too light to read is corrected and says so', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.emulateMedia({ colorScheme: 'light' });
	await page.goto('/datasets');
	const html = page.locator('html');
	await page.getByRole('button', { name: 'Account' }).click();
	await page.getByRole('menuitemradio', { name: 'System' }).click();
	await expect(html).not.toHaveClass(/dark/);
	await page.emulateMedia({ colorScheme: 'dark' });
	await expect(html).toHaveClass(/dark/);
	await page.emulateMedia({ colorScheme: 'light' });
	await expect(html).not.toHaveClass(/dark/);

	// A pale yellow cannot carry text on a light page: it is darkened until the accent and the text on it read.
	await page.getByRole('menuitem', { name: /Accent colour/ }).click();
	const dialog = page.getByRole('dialog', { name: 'Accent colour' });
	await dialog.getByRole('textbox', { name: 'Hex' }).fill('#ffe94d');
	await expect(dialog.getByTestId('accent-report')).toContainText('Light: darkened');
	await expect(html).toHaveAttribute('style', /--accent-l-light/);
	const done = dialog.getByRole('button', { name: 'Done' });
	expect(await textContrast(done)).toBeGreaterThanOrEqual(4.5);
	await done.click();
	await page.emulateMedia({ colorScheme: 'dark' });
	await expect(html).toHaveClass(/dark/);
	await page.getByRole('button', { name: 'Account' }).click();
	await page.getByRole('menuitem', { name: /Accent colour/ }).click();
	expect(await textContrast(dialog.getByRole('button', { name: 'Done' }))).toBeGreaterThanOrEqual(
		4.5,
	);
});

test('an accent reads as text on every surface the theme paints and on its own tints, for every theme and any colour', async ({
	page,
	context,
}) => {
	test.setTimeout(120_000);
	await signIn(context, person('Ada'));
	await page.goto('/datasets');
	const settled = page.getByRole('heading', { name: 'Datasets', level: 1 });
	await expect(settled).toBeVisible();
	// The themes' own accents, then colours chosen to fail: too light, too dark, no hue, the extremes.
	const accents = [null, '#ff00ff', '#ffe94d', '#888888', '#0000ff', '#000000', '#ffffff'];
	const table: Record<string, unknown>[] = [];
	const failures: string[] = [];
	for (const { id: theme } of THEMES) {
		for (const accent of accents) {
			await page.evaluate(
				(record) => localStorage.setItem('workbench-appearance', record),
				JSON.stringify({ v: 2, theme, mode: 'light', accent }),
			);
			// A reload signs in again (the session lives in the page): wait for the page it lands on.
			await page.reload();
			await expect(settled).toBeVisible();
			await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
			if (accent) await expect(page.locator('html')).toHaveAttribute('style', /--accent-h/);
			const measured = await accentOnSurfaces(page, [...ACCENT_SURFACES], [...ACCENT_TINTS]);
			table.push({ theme, accent: accent ?? 'theme', ...measured });
			for (const [mode, m] of Object.entries(measured)) {
				if (m.onSurfaces < 4.5)
					failures.push(`${theme} ${accent} ${mode}: ${m.onSurfaces} on ${m.worst}`);
				if (m.textOnAccent < 4.5)
					failures.push(`${theme} ${accent} ${mode}: text on it ${m.textOnAccent}`);
			}
		}
	}
	await test.info().attach('accent-on-surfaces.json', {
		body: JSON.stringify(table),
		contentType: 'application/json',
	});
	expect(failures).toEqual([]);
});
