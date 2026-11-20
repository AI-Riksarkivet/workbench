import { expect as baseExpect, test, type Page } from '@playwright/test';
import type { MockIdentity } from './mock-oidc';
import { AFTER_THE_WAIT_MS, adapterAnswersLate } from './adapter';
import { EDGE } from './ports';
import { callsOf, person, signIn } from './session';

// A dataset's views as panels of its one dock, the atlas, the graph and the tree, in a browser whose GPU adapter answers
// with none only after the app has started (`adapterAnswersLate`): the panels that draw with WebGPU refuse plainly and
// fetch nothing they cannot draw, and everything that does not draw still works. `webgpu.spec.ts` runs the renderers.

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

const results = (page: Page) => page.getByRole('list', { name: 'Search results' });
const tab = (page: Page, name: string) => page.getByRole('tab', { name: new RegExp(`^${name}`) });

// Every page load waits past the app's wait for the adapter, so an assertion here waits up to 10 s.
const expect = baseExpect.configure({ timeout: 10_000 });

test.beforeEach(async ({ page }) => {
	test.slow();
	await adapterAnswersLate(page, AFTER_THE_WAIT_MS, true);
});

test('the dock is the one way to every view: its menu opens a panel, and lists one the dataset cannot feed with why', async ({
	page,
	context,
}) => {
	await signIn(context, person('Barbara'));
	// A dataset that declares no view opens with its results and its player alone.
	await page.goto('/datasets/lake~acme%24notes');
	await expect(page.getByRole('region', { name: 'Player' })).toBeVisible();
	await expect(page.getByRole('tab')).toHaveCount(2);

	await page.getByRole('button', { name: 'Open a panel' }).first().click();
	const graph = page.getByRole('menuitem', { name: /^Graph/ });
	await expect(graph).toHaveAccessibleDescription('this dataset declares no knowledge graph');
	await expect(page.getByRole('menuitem', { name: /^Tree/ })).toHaveAccessibleDescription(
		'this dataset declares no topic hierarchy',
	);
	// A pointer on it opens nothing: the reason is what it gives.
	await graph.click({ force: true });
	await expect(page.getByRole('tab')).toHaveCount(2);
	// The keyboard reaches it like any item, its name the panel's alone, and one Escape closes the menu.
	await page.keyboard.press('End');
	const tree = page.getByRole('menuitem', { name: 'Tree', exact: true });
	await expect(tree).toBeFocused();
	await expect(tree).toHaveAttribute('aria-disabled', 'true');
	await page.keyboard.press('Escape');
	await expect(page.getByRole('menu')).toHaveCount(0);

	// A dataset that declares them lists them, and a closed one opens from the menu.
	await page.goto('/datasets/lake~acme%24letters');
	await expect(tab(page, 'Graph')).toBeVisible();
	await page.getByRole('button', { name: 'Close Graph' }).click();
	await expect(tab(page, 'Graph')).toHaveCount(0);
	await page.getByRole('button', { name: 'Open a panel' }).first().click();
	await page.getByRole('menuitem', { name: /^Graph/ }).click();
	await expect(tab(page, 'Graph')).toHaveAttribute('aria-selected', 'true');
	await expect(page.getByRole('combobox', { name: 'Search entities' })).toBeVisible();
});

test('a saved link to a view lands on the dock with that panel in front, or with its reason where none can be drawn', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	await page.goto('/datasets/lake~acme%24letters/tree?project=acme&table=chunks#notes');
	await expect(page).toHaveURL(
		/^[^?]*\/datasets\/lake~acme%24letters\?project=acme&table=chunks&[^#]*#notes$/,
	);
	await expect(page).toHaveTitle('lake~acme$letters · Datasets · workbench');
	await expect(tab(page, 'Tree')).toHaveAttribute('aria-selected', 'true');
	await expect(page.getByRole('button', { name: /^Collections — 4 chunks/ })).toBeVisible();

	await page.goto('/datasets/lake~acme%24notes/graph');
	await expect(tab(page, 'Graph')).toHaveAttribute('aria-selected', 'true');
	await expect(page.getByRole('region', { name: 'No graph here' })).toContainText(
		'Graph is not available: this dataset declares no knowledge graph.',
	);
});

test('a maximized atlas refuses plainly without WebGPU and downloads no points', async ({
	page,
	context,
}) => {
	const who = person('Grace');
	await signIn(context, who);
	await page.goto('/datasets/lake~acme%24letters?project=acme&max=atlas');

	await expect(page.getByRole('button', { name: 'Restore' })).toBeVisible();
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

test('picking an entity lists the rows that name it, and its clip plays in the player', async ({
	page,
	context,
}) => {
	await signIn(context, person('Barbara'));
	await page.goto('/datasets/lake~acme%24letters?panel=graph');
	await expect(page.getByText('WebGPU is required (no GPU adapter available)')).toBeVisible();
	await expect(page.getByText('Browsing 3 documents')).toBeVisible();

	// The entity search is a combobox: the arrows move through its matches, Enter picks one.
	const search = page.getByRole('combobox', { name: 'Search entities' });
	await search.fill('harbour');
	await expect(
		page.getByRole('listbox', { name: 'Matching entities' }).getByRole('option'),
	).toHaveCount(2);
	await search.press('ArrowDown');
	await expect(search).toHaveAttribute('aria-activedescendant', /graph-match-0/);
	await expect(page.getByRole('option', { name: /The harbour master/ })).toHaveAttribute(
		'aria-selected',
		'true',
	);
	await search.press('Enter');

	const panel = page.getByRole('complementary', { name: 'The harbour master' });
	await expect(panel.getByText('3 mentions')).toBeVisible();
	await expect(panel.getByRole('button', { name: 'to The harbour', exact: true })).toBeVisible();
	// The selection is the dataset's: the results search for the entity's name.
	await expect(page.getByRole('searchbox', { name: /this dataset/ })).toHaveValue(
		'The harbour master',
	);
	await expect(results(page).getByRole('listitem')).toHaveCount(1);
	await expect(page).toHaveURL(/entity=a000000000000001/);

	await panel.getByRole('button', { name: /The ships came into the harbour at dawn/ }).click();
	const player = page.getByRole('region', { name: 'Player' });
	await expect(player.getByRole('heading', { name: 'A letter from the harbour' })).toBeVisible();
	await expect(page).toHaveURL(/row=letter-001&row=0&/);
});

test('the Cypher console answers in the table, and a query it cannot run says why', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets/lake~acme%24letters?panel=graph');
	await page.getByRole('button', { name: 'Cypher', exact: true }).click();
	await page.getByRole('button', { name: /Who are the people\?/ }).click();
	const table = page.getByRole('table');
	await expect(table.getByRole('row')).toHaveCount(6);
	await expect(table).toContainText('The harbour master');

	await page.getByRole('textbox', { name: 'Cypher query' }).fill('RETURN 1');
	await page.getByRole('button', { name: 'Run', exact: true }).click();
	await expect(page.getByRole('alert')).toHaveText('Cypher: expected MATCH');
});

test('the tree drills into a branch, and a topic chosen lists its rows in the results', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	await page.goto('/datasets/lake~acme%24letters?panel=tree&project=acme&table=chunks');

	await page.getByRole('button', { name: /^Collections — 4 chunks/ }).click();
	await page.getByRole('button', { name: /^Letters — 3 chunks/ }).focus();
	await page.keyboard.press('Enter');

	await expect(results(page).getByRole('listitem')).toHaveCount(3);
	await expect(
		page.getByRole('status').filter({ hasText: 'The results list the topic' }),
	).toHaveText(/Letters/);
	await expect(page).toHaveURL(/topic=Letters/);
	// The drill is kept while the topic is shown.
	await expect(page.getByRole('button', { name: /^Letters — 3 chunks/ })).toBeVisible();

	await page.getByRole('button', { name: 'Clear the topic Letters' }).click();
	await expect(page.getByText('Browsing 3 documents')).toBeVisible();
	await expect(page).not.toHaveURL(/topic=/);
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

	await page.goto('/datasets/lake~acme%24letters?panel=atlas');
	await expect(page.getByText('No embedding map yet')).toBeVisible();
	await page.goto('/datasets/lake~acme%24letters?panel=graph');
	await expect(page.getByText('No knowledge graph yet')).toBeVisible();
	await page.goto('/datasets/lake~acme%24letters?panel=tree');
	await expect(page.getByText('No topics yet')).toBeVisible();

	for (const path of ['/api/atlas/status', '/api/graph/status', '/api/topics']) {
		await fault(page, who, path, 'fail');
	}
	for (const [panel, what] of [
		['atlas', 'The embedding map could not be loaded.'],
		['graph', 'The knowledge graph could not be loaded.'],
		['tree', 'The topics could not be loaded.'],
	] as const) {
		await page.goto(`/datasets/lake~acme%24letters?panel=${panel}`);
		await expect(page.getByRole('alert')).toHaveText(what);
		await expect(page.getByText('Details: the viewer could not read the table')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
	}

	await page.goto('/datasets/lake~acme%24nope/graph');
	await expect(page.getByRole('alert')).toContainText('No dataset lake~acme$nope that you can see');
});

test('a failed atlas or graph read is tried again, and the panel works once it answers', async ({
	page,
	context,
}) => {
	const who = person('Edsger');
	await signIn(context, who);
	for (const [panel, door, after] of [
		['atlas', '/api/atlas/status', 'WebGPU is required (no GPU adapter available)'],
		['graph', '/api/graph/status', 'WebGPU is required (no GPU adapter available)'],
	] as const) {
		await fault(page, who, door, 'fail');
		await page.goto(`/datasets/lake~acme%24letters?panel=${panel}`);
		await expect(page.getByRole('alert')).toBeVisible();
		await fault(page, who, door, 'clear');
		await page.getByRole('button', { name: 'Try again' }).click();
		await expect(page.getByRole('alert')).toHaveCount(0);
		await expect(page.getByText(after)).toBeVisible();
	}
	// The graph is usable again: its search finds an entity.
	await page.getByRole('combobox', { name: 'Search entities' }).fill('fleet');
	await expect(page.getByRole('option', { name: /The fleet/ })).toBeVisible();
});

test('another dataset’s graph is that dataset’s, and a late answer for the first is not drawn on it', async ({
	page,
	context,
}) => {
	const who = person('Grace');
	await signIn(context, who);
	await page.goto('/datasets/lake~acme%24letters?panel=graph');
	await page.getByRole('combobox', { name: 'Search entities' }).fill('harbour');
	// The entity answers 2.5 s late; the reader moves to another dataset before it does.
	await fault(page, who, '/api/graph/entity/a000000000000001', 'slow');
	const late = page.waitForResponse(
		(res) => res.url().includes('/api/graph/entity/a000000000000001'),
		{
			timeout: 10_000,
		},
	);
	await page.getByRole('option', { name: /The harbour master/ }).click();
	const mapsOverview = page.waitForResponse(
		(res) =>
			res.url().includes('/api/graph/subgraph') && res.url().includes('dataset=lake%7Eacme%24maps'),
	);
	await gotoInPage(page, '/datasets/lake~acme%24maps?panel=graph');
	// The second dataset's graph has loaded, on its own page, before its entities are searched.
	await mapsOverview;
	await expect(page).toHaveTitle(/^lake~acme\$maps /);

	await page.getByRole('combobox', { name: 'Search entities' }).fill('the');
	await expect(page.getByRole('option', { name: /The coast/ })).toBeVisible();
	await expect(page.getByRole('option', { name: /The harbour/ })).toHaveCount(0);
	// The first dataset's answer arrives while the second's page is up, and draws nothing on it.
	await late;
	// The answer has been handed to the page: two frames later, anything it would draw is drawn.
	await page.evaluate(
		() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))),
	);
	await expect(page.getByRole('complementary', { name: 'The harbour master' })).toHaveCount(0);
	const subgraphs = (await callsOf(page, who)).filter((c) => c.path === '/api/graph/subgraph');
	expect(subgraphs.at(-1)?.query).toContain('dataset=lake%7Eacme%24maps');
});

test('a reload, or the link sent to someone else, shows the search, the row opened and the panel maximized', async ({
	page,
	context,
	browser,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets/lake~acme%24letters');
	await page.getByRole('searchbox', { name: /this dataset/ }).fill('harbour');
	await page.getByRole('button', { name: 'Search', exact: true }).click();
	await expect(results(page).getByRole('listitem')).toHaveCount(3);
	await results(page)
		.getByRole('button', { name: /The harbour master counted twelve/ })
		.click();
	const player = page.getByRole('region', { name: 'Player' });
	await expect(player.getByText('Page 2 of 2')).toBeVisible();
	await page.getByRole('button', { name: 'Maximize Player' }).click();
	await expect(page).toHaveURL(/q=harbour.*row=letter-001&row=1.*max=player/);

	await page.reload();
	await expect(page.getByRole('button', { name: 'Restore' })).toBeVisible();
	await expect(player.getByText('Page 2 of 2')).toBeVisible();
	await page.getByRole('button', { name: 'Restore' }).click();
	await expect(page.getByRole('searchbox', { name: /this dataset/ })).toHaveValue('harbour');
	await expect(results(page).getByRole('listitem')).toHaveCount(3);

	// Another person, on another browser with no layout of theirs, lands on the same view.
	const link = page.url();
	const other = await browser.newContext();
	await signIn(other, person('Grace'));
	const theirs = await other.newPage();
	await theirs.goto(link);
	await expect(theirs.getByRole('searchbox', { name: /this dataset/ })).toHaveValue('harbour');
	await expect(
		theirs.getByRole('region', { name: 'Player' }).getByText('Page 2 of 2'),
	).toBeVisible();
	await other.close();
});

test('a failed graph read is asked once, and Try again asks exactly once more', async ({
	page,
	context,
}) => {
	const who = person('Barbara');
	await signIn(context, who);
	await fault(page, who, '/api/graph/subgraph', 'fail');
	await page.goto('/datasets/lake~acme%24letters?panel=graph');
	await expect(page.getByRole('alert')).toHaveText('The graph could not be loaded.');
	const subgraphs = async () =>
		(await callsOf(page, who)).filter((call) => call.path === '/api/graph/subgraph').length;
	// Long enough for a loop to have asked many times over.
	await page.waitForTimeout(1500);
	expect(await subgraphs()).toBe(1);

	await page.getByRole('button', { name: 'Try again' }).click();
	await expect.poll(subgraphs).toBe(2);
	await page.waitForTimeout(1500);
	expect(await subgraphs()).toBe(2);
});

test('moving to another dataset and Back returns to the first dataset’s own view', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets/lake~acme%24letters');
	await page.getByRole('searchbox', { name: /this dataset/ }).fill('harbour');
	await page.getByRole('button', { name: 'Search', exact: true }).click();
	await results(page)
		.getByRole('button', { name: /The harbour master counted twelve/ })
		.click();
	await expect(page).toHaveURL(/q=harbour.*row=letter-001&row=1/);
	const first = page.url();

	await gotoInPage(page, '/datasets/lake~acme%24maps?panel=graph&entity=b000000000000001');
	await expect(page.getByRole('complementary', { name: 'The coast' })).toBeVisible();
	await page.goBack();

	await expect(page).toHaveTitle(/^lake~acme\$letters /);
	await expect(page).toHaveURL(first);
	await expect(page.getByRole('searchbox', { name: /this dataset/ })).toHaveValue('harbour');
	await expect(page.getByRole('region', { name: 'Player' }).getByText('Page 2 of 2')).toBeVisible();
});

test('a link to the dataset already open brings in front what it names: an entity, a panel, a maximized one', async ({
	page,
	context,
}) => {
	await signIn(context, person('Grace'));
	await page.goto('/datasets/lake~acme%24letters?panel=graph');
	await expect(page.getByText('WebGPU is required (no GPU adapter available)')).toBeVisible();
	await gotoInPage(page, '/datasets/lake~acme%24letters?entity=a000000000000004&max=tree');
	await expect(page.getByRole('button', { name: 'Restore' })).toBeVisible();
	await expect(page.getByRole('heading', { name: 'Tree', exact: true, level: 2 })).toBeAttached();
	await page.getByRole('button', { name: 'Restore' }).click();
	await page.getByRole('tab', { name: /^Graph/ }).click();
	await expect(page.getByRole('complementary', { name: 'The fleet' })).toBeVisible();
});

test('a link’s row waits for the link’s search in the address, and a newer search drops it', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	let release: () => void = () => {};
	const held = new Promise<void>((done) => (release = done));
	let first = true;
	await page.route('**/api/search/api/search?*', async (route) => {
		if (first) {
			first = false;
			await held;
		}
		await route.continue();
	});
	await page.goto('/datasets/lake~acme%24letters?q=harbour&row=letter-001&row=1');
	await expect(page.getByRole('searchbox', { name: /this dataset/ })).toHaveValue('harbour');
	// The search has not answered: the address still names the row the link opened.
	await expect(page).toHaveURL(/row=letter-001&row=1/);

	await page.getByRole('searchbox', { name: /this dataset/ }).fill('fleet');
	await page.getByRole('button', { name: 'Search', exact: true }).click();
	release();
	await expect(page.getByRole('searchbox', { name: /this dataset/ })).toHaveValue('fleet');
	await expect(page).not.toHaveURL(/row=/);
	await expect(
		page.getByRole('region', { name: 'Player' }).getByText('Click a hit to play.'),
	).toBeVisible();
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
	await page.waitForURL((url) => `${url.pathname}${url.search}`.startsWith(path.split('?')[0]!));
	expect(
		await page.evaluate(() => (window as unknown as { __sameDocument?: boolean }).__sameDocument),
	).toBe(true);
}
