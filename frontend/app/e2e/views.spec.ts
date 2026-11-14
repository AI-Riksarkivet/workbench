import { expect, test, type Page } from '@playwright/test';
import type { MockIdentity } from './mock-oidc';
import { EDGE } from './ports';
import { callsOf, person, signIn } from './session';

// The dataset's views beside its dock, the atlas, the graph and the tree, in a browser with no WebGPU
// adapter (headless Chromium offers none): the pages that draw with WebGPU refuse plainly and fetch
// nothing they cannot draw, and everything that does not draw still works. `webgpu.spec.ts` runs the
// renderers.

/** Make one viewer door fail, or answer that nothing is built there, for this person only. */
async function fault(
	page: Page,
	who: MockIdentity,
	path: string,
	kind: 'fail' | 'unbuilt' | 'slow' | 'clear',
) {
	const params = new URLSearchParams({ sub: who.sub, path, kind });
	await page.request.post(`${EDGE}/api/viewer/__fault?${params}`);
}

test('the atlas page refuses plainly without WebGPU and downloads no points', async ({
	page,
	context,
}) => {
	const who = person('Grace');
	await signIn(context, who);
	await page.goto('/datasets/acme%24letters?project=acme');
	await page
		.getByRole('navigation', { name: 'Dataset pages' })
		.getByRole('link', { name: 'Atlas' })
		.click();

	await expect(page).toHaveURL('/datasets/acme%24letters/atlas?project=acme');
	await expect(page).toHaveTitle('Atlas · acme$letters · Datasets · workbench');
	await expect(page.getByRole('heading', { level: 1 })).toHaveText('Atlas');
	await expect(
		page.getByRole('navigation', { name: 'Dataset pages' }).getByRole('link', { name: 'Atlas' }),
	).toHaveAttribute('aria-current', 'page');
	await expect(page.getByText('WebGPU is required (no GPU adapter available)')).toBeVisible();
	await expect(
		page.getByText('Lasso a region, or pick a cluster in the legend, to list its rows.'),
	).toBeVisible();
	await expect(
		page.getByText('Click a point on the map, or a row of the selection, to play it.'),
	).toBeVisible();
	const calls = await callsOf(page, who);
	expect(calls.map((call) => call.path)).not.toContain('/api/atlas/points');
	// Only the space the dataset declares is asked about.
	const statuses = calls.filter((call) => call.path === '/api/atlas/status');
	expect(statuses.length).toBeGreaterThan(0);
	for (const call of statuses) expect(call.query).toContain('space=umap');
});

test('the graph reads its entities without WebGPU: a search opens one, and a clip plays it', async ({
	page,
	context,
}) => {
	await signIn(context, person('Barbara'));
	await page.goto('/datasets/acme%24letters/graph');
	await expect(page).toHaveTitle('Graph · acme$letters · Datasets · workbench');
	await expect(page.getByText('WebGPU is required (no GPU adapter available)')).toBeVisible();

	// The match list is reached from the search box by the keyboard.
	await page.getByRole('searchbox', { name: 'Search entities' }).fill('harbour');
	await expect(
		page.getByRole('list', { name: 'Matching entities' }).getByRole('listitem'),
	).toHaveCount(2);
	await page.keyboard.press('Tab');
	await expect(page.getByRole('button', { name: /The harbour master/ })).toBeFocused();
	await page.keyboard.press('Enter');

	const panel = page.getByRole('complementary', { name: 'The harbour master' });
	await expect(panel.getByText('3 mentions')).toBeVisible();
	await expect(panel.getByRole('button', { name: 'to The harbour', exact: true })).toBeVisible();
	await panel.getByRole('button', { name: /The ships came into the harbour at dawn/ }).click();
	await expect(page).toHaveURL(/\/datasets\/acme%24letters\?doc=letter-001&t=12$/);
	await expect(page.getByRole('region', { name: 'Player' })).toBeVisible();
});

test('the Cypher console answers in the table, and a query it cannot run says why', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets/acme%24letters/graph');
	await page.getByRole('button', { name: 'Cypher', exact: true }).click();
	await page.getByRole('button', { name: /Who are the people\?/ }).click();
	const table = page.getByRole('table');
	await expect(table.getByRole('row')).toHaveCount(6);
	await expect(table).toContainText('The harbour master');

	await page.getByRole('textbox', { name: 'Cypher query' }).fill('RETURN 1');
	await page.getByRole('button', { name: 'Run', exact: true }).click();
	await expect(page.getByRole('alert')).toHaveText('Cypher: expected MATCH');
});

test('the tree drills into a branch, lists a topic’s rows, and opens them in search', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	await page.goto('/datasets/acme%24letters/tree?project=acme&table=chunks');
	await expect(page).toHaveTitle('Tree · acme$letters · Datasets · workbench');

	await page.getByRole('button', { name: /^Collections — 4 chunks/ }).click();
	await page.getByRole('button', { name: /^Letters — 3 chunks/ }).focus();
	await page.keyboard.press('Enter');

	const rows = page.getByRole('region', { name: 'Letters' });
	await expect(
		rows.getByRole('list', { name: 'Search results' }).getByRole('listitem'),
	).toHaveCount(3);
	await rows.getByRole('link', { name: 'Open in search' }).click();
	await expect(page).toHaveURL('/datasets/acme%24letters?topic=Letters&project=acme&table=chunks');
	await expect(
		page.getByRole('list', { name: 'Search results' }).getByRole('listitem'),
	).toHaveCount(3);
});

test('each view says when nothing is built yet, and when its door fails', async ({
	page,
	context,
}) => {
	const who = person('Margaret');
	await signIn(context, who);
	for (const path of ['/api/atlas/status', '/api/graph/status', '/api/topics']) {
		await fault(page, who, path, 'unbuilt');
	}

	await page.goto('/datasets/acme%24letters/atlas');
	await expect(page.getByText('No embedding map yet')).toBeVisible();
	await page.goto('/datasets/acme%24letters/graph');
	await expect(page.getByText('No knowledge graph yet')).toBeVisible();
	await page.goto('/datasets/acme%24letters/tree');
	await expect(page.getByText('No topics yet')).toBeVisible();

	for (const path of ['/api/atlas/status', '/api/graph/status', '/api/topics']) {
		await fault(page, who, path, 'fail');
	}
	for (const [path, what] of [
		['atlas', 'The embedding map could not be loaded.'],
		['graph', 'The knowledge graph could not be loaded.'],
		['tree', 'The topics could not be loaded.'],
	] as const) {
		await page.goto(`/datasets/acme%24letters/${path}`);
		await expect(page.getByRole('alert')).toHaveText(what);
		await expect(page.getByText('Details: the viewer could not read the table')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
	}

	await page.goto('/datasets/acme%24nope/graph');
	await expect(page.getByRole('alert')).toContainText('No dataset acme$nope that you can see');
});

test('a failed atlas or graph read is tried again, and the page works once it answers', async ({
	page,
	context,
}) => {
	const who = person('Edsger');
	await signIn(context, who);
	for (const [path, door, after] of [
		['atlas', '/api/atlas/status', 'WebGPU is required (no GPU adapter available)'],
		['graph', '/api/graph/status', 'WebGPU is required (no GPU adapter available)'],
	] as const) {
		await fault(page, who, door, 'fail');
		await page.goto(`/datasets/acme%24letters/${path}`);
		await expect(page.getByRole('alert')).toBeVisible();
		await fault(page, who, door, 'clear');
		await page.getByRole('button', { name: 'Try again' }).click();
		await expect(page.getByRole('alert')).toHaveCount(0);
		await expect(page.getByText(after)).toBeVisible();
	}
	// The graph is usable again: its search finds an entity.
	await page.getByRole('searchbox', { name: 'Search entities' }).fill('fleet');
	await expect(page.getByRole('button', { name: /The fleet/ })).toBeVisible();
});

test('another dataset’s graph is that dataset’s, and a late answer for the first is not drawn on it', async ({
	page,
	context,
}) => {
	const who = person('Grace');
	await signIn(context, who);
	await page.goto('/datasets/acme%24letters/graph');
	await page.getByRole('searchbox', { name: 'Search entities' }).fill('harbour');
	// The entity answers 2.5 s late; the reader moves to another dataset before it does.
	await fault(page, who, '/api/graph/entity/a000000000000001', 'slow');
	const late = page.waitForResponse(
		(res) => res.url().includes('/api/graph/entity/a000000000000001'),
		{
			timeout: 10_000,
		},
	);
	await page.getByRole('button', { name: /The harbour master/ }).click();
	const mapsOverview = page.waitForResponse(
		(res) => res.url().includes('/api/graph/subgraph') && res.url().includes('dataset=acme%24maps'),
	);
	await gotoInPage(page, '/datasets/acme%24maps/graph');
	// The second dataset's graph has loaded before its entities are searched.
	await mapsOverview;
	await expect(page).toHaveURL(/\/datasets\/acme%24maps\/graph/);

	await page.getByRole('searchbox', { name: 'Search entities' }).fill('the');
	await expect(page.getByRole('button', { name: /The coast/ })).toBeVisible();
	await expect(page.getByRole('button', { name: /The harbour/ })).toHaveCount(0);
	// The first dataset's answer arrives while the second's page is up, and draws nothing on it.
	await late;
	// The answer has been handed to the page: two frames later, anything it would draw is drawn.
	await page.evaluate(
		() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))),
	);
	await expect(page.getByRole('complementary', { name: 'The harbour master' })).toHaveCount(0);
	const subgraphs = (await callsOf(page, who)).filter((c) => c.path === '/api/graph/subgraph');
	expect(subgraphs.at(-1)?.query).toContain('dataset=acme%24maps');
});

/** Move to `path` client-side, as a link inside the app would, without leaving the dataset's layout. */
async function gotoInPage(page: Page, path: string): Promise<void> {
	await page.evaluate((href) => {
		(window as unknown as { __sameDocument?: boolean }).__sameDocument = true;
		const link = document.createElement('a');
		link.href = href;
		link.textContent = 'move';
		document.body.append(link);
		link.click();
	}, path);
	await page.waitForURL(`**${path}`);
	expect(
		await page.evaluate(() => (window as unknown as { __sameDocument?: boolean }).__sameDocument),
	).toBe(true);
}
