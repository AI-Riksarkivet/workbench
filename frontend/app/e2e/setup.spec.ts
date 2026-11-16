import { expect, test, type Locator, type Page } from '@playwright/test';
import { drawnPixels } from './canvas-pixels';
import { callsOf, person, signIn, watchCsp } from './session';

// A table nobody stamped feeds the Atlas, the Graph and the Tree once a person picks its columns
// (`#lib/plain/SetupForm.svelte`, the viewer's derived doors mocked in `mock-services.ts`): the form opens on
// what the schema suggests, nothing is drawn until it is confirmed, the Graph and the Tree say what their
// columns hold first (from the check doors, never the whole view), a node lists the rows its count counted
// (from the node's own door, by its key), and the set-ups are kept in a view and carried by a link. Drawn on
// the WebGPU project's software adapter.

const DATASET = '/datasets/archive~field%24survey';

async function openPanel(page: Page, name: 'Atlas' | 'Graph' | 'Tree'): Promise<void> {
	await page.getByRole('button', { name: 'Open a panel' }).last().click();
	const item = page.getByRole('menuitem', { name: new RegExp(`^${name}`) });
	await expect(item).toContainText('Set up…');
	await item.click();
}

test('the atlas opens on the vector the schema suggests, draws once confirmed, and its link carries the set-up', async ({
	page,
	context,
}) => {
	const who = person('Ada');
	await signIn(context, who);
	const violations = await watchCsp(page);
	await page.goto(DATASET);

	await openPanel(page, 'Atlas');
	const form = page.getByRole('form', { name: 'Set up the atlas' });
	await expect(form.getByRole('combobox', { name: /^Vector/ })).toHaveValue('embedding');
	await expect(form.getByRole('combobox', { name: /^Label/ })).toHaveValue('note');
	await expect(form.getByRole('combobox', { name: /^Colour by/ })).toHaveValue('');
	expect((await callsOf(page, who)).filter((c) => c.path.endsWith('/atlas'))).toEqual([]);

	await form.getByRole('combobox', { name: /^Colour by/ }).selectOption('site');
	await form.getByRole('button', { name: 'Draw the atlas' }).click();

	const atlas = page.getByRole('region', { name: 'Atlas' });
	await expect(atlas.getByRole('status').first()).toContainText(
		'4 of 4 rows · embedding projected (PCA, 91% of the variance)',
	);
	// One of the four points sits in the middle of the map, where the pixels are read.
	await expect.poll(() => drawnPixels(page, atlas.locator('canvas').first())).toBeGreaterThan(10);
	await expect(page).toHaveURL(/av=embedding&ac=site&al=note/);
	const asked = (await callsOf(page, who)).find((c) => c.path.endsWith('/atlas'));
	expect(asked?.query).toBe('?vector=embedding&color=site&label=note&version=3');
	expect(await violations()).toEqual([]);
});

test('the graph says what its columns hold before it is drawn, and a node lists the rows that name it', async ({
	page,
	context,
}) => {
	const who = person('Grace');
	await signIn(context, who);
	await page.goto(DATASET);

	await openPanel(page, 'Graph');
	const form = page.getByRole('form', { name: 'Set up the graph' });
	await expect(form.getByRole('combobox', { name: /^From/ })).toHaveValue('src');
	await expect(form.getByRole('combobox', { name: /^To/ })).toHaveValue('dst');
	const check = form.getByRole('status');
	await expect(check).toContainText('Distinct nodes: 4; distinct edges: 4');
	await expect(check).toContainText('“To” values that never appear as a “from”: 1');
	// The form asked the check door alone: a draft never builds the whole view.
	const asked = (await callsOf(page, who)).map((c) => c.path.split('/tables/observations/')[1]);
	expect(asked.filter((p) => p?.startsWith('graph'))).toEqual(['graph/check']);
	// The drawn graph's read stopped short: its status says how far it read, and why.
	await page.route('**/tables/observations/graph?*', async (route) => {
		const answer = await route.fetch();
		const body = await answer.json();
		body.diagnostics.extent = {
			total: 4,
			read: 2,
			sampled: true,
			cut: 'the read stopped after 10 s',
		};
		await route.fulfill({ response: answer, json: body });
	});
	await form.getByRole('button', { name: 'Draw the graph' }).click();

	const graph = page.getByRole('region', { name: 'Graph' });
	await expect(graph.getByRole('status').first()).toHaveText(
		'4 nodes and 4 edges drawn · from 2 of 4 rows: the read stopped after 10 s',
	);
	await expect.poll(() => drawnPixels(page, graph.locator('canvas').first())).toBeGreaterThan(50);

	// A node's rows, as a link to its node (by its typed key) names them: the node's own door lists them.
	await page.goto(
		`${page.url().split('?')[0]}?${new URL(page.url()).searchParams}&node=s%3AKalmar&nodeof=graph`,
	);
	const rows = page.getByRole('region', { name: 'Rows' });
	await expect(rows.getByRole('status').last()).toHaveText('1–2 of 2 rows naming Kalmar');
	await expect(page.getByRole('heading', { name: 'Kalmar' })).toBeVisible();
	const listed = (await callsOf(page, who)).filter((c) => c.path.endsWith('/graph/rows'));
	expect(listed.at(-1)?.query).toBe(
		'?shape=edges&source=src&target=dst&weight=n&key=s%3AKalmar&offset=0&limit=50&version=3',
	);
});

test('a tree nests the rows by their path, and choosing a node lists the rows at or under it', async ({
	page,
	context,
}) => {
	const who = person('Edsger');
	await signIn(context, who);
	await page.goto(DATASET);

	await openPanel(page, 'Tree');
	const form = page.getByRole('form', { name: 'Set up the tree' });
	await expect(form.getByRole('combobox', { name: /^Path/ })).toHaveValue('place');
	await expect(form.getByRole('status')).toContainText('Nodes: 7; levels: 3');
	await form.getByRole('button', { name: 'Draw the tree' }).click();

	const tree = page.getByRole('region', { name: 'Tree' });
	await tree.getByRole('button', { name: /^Sverige — 4 rows/ }).click();
	await tree.getByRole('button', { name: /^Gotland — 2 rows/ }).click();
	await tree.getByRole('button', { name: /^Visby — 2 rows/ }).click();
	const rows = page.getByRole('region', { name: 'Rows' });
	await expect(rows.getByRole('status').last()).toHaveText('1–2 of 2 rows at or under Visby');
	await expect(page).toHaveURL(
		new RegExp(
			`node=${encodeURIComponent(JSON.stringify({ path: ['Sverige', 'Gotland', 'Visby'] }))}&nodeof=tree`,
		),
	);
});

test('a view keeps the set-ups and opens with the dataset, a link draws them in a browser that never saved them, and a missing column, a missing table or a stamped dataset is reported', async ({
	page,
	context,
	browser,
}) => {
	const who = person('Barbara');
	await signIn(context, who);
	await page.goto(DATASET);
	await openPanel(page, 'Atlas');
	await page.getByRole('button', { name: 'Draw the atlas' }).click();
	await page.getByRole('button', { name: 'New view' }).click();
	await page.getByLabel('View name').fill('Embedding');
	await page.getByLabel('View name').press('Enter');
	await page.getByRole('button', { name: 'Embedding: the default view' }).click();
	const link = page.url();

	// The dataset opened afresh opens with its default view, set-up and all.
	await page.goto(DATASET);
	await expect(page).toHaveURL(/av=embedding/);
	await expect(
		page.getByRole('region', { name: 'Atlas' }).getByRole('status').first(),
	).toContainText('4 of 4 rows');

	// Another browser, which saved nothing, draws the same atlas from the link alone.
	const other = await browser.newContext();
	await signIn(other, person('Stranger'));
	const fresh = await other.newPage();
	await fresh.goto(link);
	await expect(
		fresh.getByRole('region', { name: 'Atlas' }).getByRole('status').first(),
	).toContainText('4 of 4 rows');

	// A set-up naming a column the table does not hold reads nothing and says so.
	await fresh.goto(`${DATASET}?panel=atlas&ax=east&ay=north`);
	const atlas = fresh.getByRole('region', { name: 'Atlas' });
	await expect(atlas.getByRole('alert')).toContainText(
		'Its set-up names columns the table no longer holds: east, north.',
	);
	await atlas.getByRole('button', { name: 'Set up again' }).click();
	await expect(fresh.getByRole('form', { name: 'Set up the atlas' })).toBeVisible();

	// A set-up a link cannot be trusted with is left out, with the reason.
	await fresh.goto(`${DATASET}?panel=graph&gs=sideways&gf=src&gt=dst`);
	await expect(fresh.getByRole('status').filter({ hasText: 'set-up was not used' })).toContainText(
		'the graph set-up is not one the workbench can read',
	);

	// A link naming a table the dataset does not hold shows the one it holds, and says so.
	await fresh.goto(`${DATASET}?table=gone&panel=results`);
	await expect(fresh.getByRole('region', { name: 'Rows' }).getByRole('status').first()).toHaveText(
		'This dataset holds no table gone: showing observations.',
	);

	// A set-up carried to a stamped dataset, whose descriptor draws its views, is not used, and said.
	await fresh.goto('/datasets/lake~acme%24letters?panel=atlas&ax=east&ay=north');
	await expect(fresh.getByRole('status').filter({ hasText: 'set-up was not used' })).toContainText(
		'apply to a table with no workbench descriptor, and this dataset has one',
	);
	await other.close();
});

// The demo's own plain tables, answered as the viewer answered them (`e2e/seeded-answers.json`, recorded from the
// real viewer by `scripts/record_derived_answers.py` and held to it by the viewer's suite): the suggested set-ups
// are drawn, not only filled in, so an answer the app cannot read fails here.

/** Draw `name` from the form's suggestion `lead`, and wait for its status line: drawn, and nothing reported failing. */
async function drawSuggested(
	page: Page,
	name: 'Atlas' | 'Graph' | 'Tree',
	lead: RegExp,
	status: string,
): Promise<Locator> {
	await openPanel(page, name);
	const form = page.getByRole('form', { name: `Set up the ${name.toLowerCase()}` });
	await form.getByRole('button', { name: lead }).click();
	await form.getByRole('button', { name: `Draw the ${name.toLowerCase()}` }).click();
	const region = page.getByRole('region', { name });
	await expect(region.getByRole('status').first()).toContainText(status);
	await expect(region.getByRole('alert')).toHaveCount(0);
	return region;
}

test('the demo survey draws its suggested atlas, graph and tree, and a port no cargo leaves lists the rows that name it', async ({
	page,
	context,
}) => {
	const who = person('Mary');
	await signIn(context, who);
	const errors: string[] = [];
	page.on('pageerror', (error) => errors.push(error.message));
	await page.goto('/datasets/seeded~field%24survey');

	const graph = await drawSuggested(
		page,
		'Graph',
		/^shipped_from → shipped_to/,
		'5 nodes and 8 edges drawn',
	);
	await expect.poll(() => drawnPixels(page, graph.locator('canvas').first())).toBeGreaterThan(50);
	// Lübeck is only ever shipped to: the rows naming it are the rows whose “to” holds it.
	await page.goto(`${page.url()}&node=s%3AL%C3%BCbeck&nodeof=graph`);
	const rows = page.getByRole('region', { name: 'Rows' });
	await expect(rows.getByRole('status').last()).toHaveText('1–12 of 12 rows naming Lübeck');

	const tree = await drawSuggested(page, 'Tree', /^place \(split at/, '12 nodes, 4 levels');
	await tree.getByRole('button', { name: /^Sverige — 60 rows/ }).click();
	await tree.getByRole('button', { name: /^Götaland — 30 rows/ }).click();
	await tree.getByRole('button', { name: /^Gotland — 15 rows/ }).click();
	await tree.getByRole('button', { name: /^Visby — 15 rows/ }).click();
	await expect(rows.getByRole('status').last()).toHaveText('1–15 of 15 rows at or under Visby');

	const atlas = await drawSuggested(page, 'Atlas', /^embedding/, '60 of 60 rows');
	await expect.poll(() => drawnPixels(page, atlas.locator('canvas').first())).toBeGreaterThan(10);
	expect(errors).toEqual([]);
	expect((await callsOf(page, who)).filter((c) => c.status !== 200)).toEqual([]);
});

test('the demo scans draw their citations as a graph and a tree, the scan citing one the archive lacks at the top', async ({
	page,
	context,
}) => {
	const who = person('Ida');
	await signIn(context, who);
	await page.goto('/datasets/loose~');

	const graph = await drawSuggested(
		page,
		'Graph',
		/^scan → cites/,
		'3 nodes and 1 edges drawn · 1 references match no row',
	);
	await expect
		.poll(() => drawnPixels(page, graph.locator('canvas').first(), { whole: true }))
		.toBeGreaterThan(10);

	const tree = await drawSuggested(
		page,
		'Tree',
		/^scan, each under its cites/,
		'1 orphans at the top',
	);
	await tree.getByRole('button', { name: /^Domboksregister 1688 — 1 rows?/ }).click();
	const rows = page.getByRole('region', { name: 'Rows' });
	await expect(rows.getByRole('status').last()).toHaveText(
		'1–1 of 1 rows at or under Domboksregister 1688',
	);
	expect((await callsOf(page, who)).filter((c) => c.status !== 200)).toEqual([]);
});
