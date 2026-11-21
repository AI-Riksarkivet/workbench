import { expect, test, type Page } from '@playwright/test';
import { textContrast } from './contrast';
import { serveRecording } from './recording';
import { person, signIn } from './session';
import { EDGE } from './ports';

// The explorer's features on the dataset page, on the built app: browsing before a search, the three
// layouts, saved views, a topic link, the keyboard, and the states a reader meets
// when a read fails or a dataset is not there.

const results = (page: Page) => page.getByRole('list', { name: 'Search results' });

async function searchFor(page: Page, q: string): Promise<void> {
	await page.getByRole('searchbox').fill(q);
	await page.getByRole('button', { name: 'Search', exact: true }).click();
}

test('before a search the dataset’s documents are browsed, and one opens in the player', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets/lake~acme%24letters');

	const documents = page.getByRole('list', { name: 'Documents' });
	await expect(page.getByText('Browsing 3 documents')).toBeVisible();
	await expect(documents.getByRole('listitem')).toHaveCount(3);
	// Each row carries its preview and its metadata with the label that says what the value is.
	const first = documents.getByRole('listitem').first();
	await expect(first).toContainText('Collection: Letters');
	await expect
		.poll(() => first.locator('img').evaluate((img: HTMLImageElement) => img.naturalWidth))
		.toBeGreaterThan(0);
	await documents.getByRole('button', { name: /A map of the harbour/ }).click();

	// The player names what it opened (the picture itself is the WebGPU project's, `player.spec.ts`).
	const player = page.getByRole('region', { name: 'Player' });
	await expect(player.getByRole('heading', { name: 'A map of the harbour' })).toBeVisible();
});

test('a recording whose media and transcript both fail says so, and one Try again brings both back', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	// The stand-in's dataset as a timed recording with a transcript.
	await page.route('**/api/viewer/api/datasets/*/descriptor', async (route) => {
		const answer = await route.fetch();
		const body = await answer.json();
		body.declared.document.kind = 'audio';
		body.declared.time = { start: 'start', end: 'end' };
		body.declared.capabilities = { ...body.declared.capabilities, alignments: 'chunks.alignments' };
		await route.fulfill({ response: answer, json: body });
	});
	const failing = ['**/api/viewer/api/signed-media', '**/api/viewer/api/doc-chunks/**'];
	for (const pattern of failing)
		await page.route(pattern, (route) =>
			route.request().method() === 'POST' || pattern.includes('doc-chunks')
				? route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } })
				: route.continue(),
		);
	await page.goto('/datasets/lake~acme%24letters');
	await page
		.getByRole('list', { name: 'Documents' })
		.getByRole('button', { name: /A map of the harbour/ })
		.click();

	const player = page.getByRole('region', { name: 'Player' });
	await expect(player.getByRole('alert')).toHaveText("This item's media could not be loaded.");
	await expect(player.locator('audio')).toHaveCount(0);

	for (const pattern of failing) await page.unroute(pattern);
	await serveRecording(page);
	await player.getByRole('button', { name: 'Try again' }).click();
	await expect(player.locator('audio')).toHaveAttribute('src', /\/api\/signed-media\//);
	await expect(player.getByRole('alert')).toHaveCount(0);
	await expect(player.getByRole('region', { name: 'Transcript' })).toBeVisible();
});

test("a diarized recording's Speakers tab names it a recording", async ({ page, context }) => {
	await signIn(context, person('Ada'));
	// The stand-in's letters as a diarized recording.
	await page.route('**/api/viewer/api/datasets/*/descriptor', async (route) => {
		const answer = await route.fetch();
		const body = await answer.json();
		body.declared.document.kind = 'audio';
		body.declared.capabilities = { ...body.declared.capabilities, diarization: 'turns' };
		await route.fulfill({ response: answer, json: body });
	});
	await page.goto('/datasets/lake~acme%24letters');

	await page
		.getByRole('list', { name: 'Documents' })
		.getByRole('button', { name: /A map of the harbour/ })
		.click();
	const player = page.getByRole('region', { name: 'Player' });
	await player.getByRole('button', { name: 'Speakers' }).click();
	// A recording is named a recording, not a video.
	await expect(player.getByText('No speaker turns for this recording.')).toBeVisible();
});

for (const { kind, shows } of [
	{ kind: null, shows: "the document's image" },
	{ kind: 'audio', shows: 'an icon of its kind' },
] as const) {
	test(`with no thumbnail declared, a ${kind ?? 'still-image'} document's tile shows ${shows}`, async ({
		page,
		context,
	}) => {
		await signIn(context, person('Ada'));
		// The stand-in's dataset as one that declares no thumbnail (and, for the second case, holds audio).
		await page.route('**/api/viewer/api/datasets/*/descriptor', async (route) => {
			const answer = await route.fetch();
			const body = await answer.json();
			delete body.declared.document.thumbnail;
			if (kind) body.declared.document.kind = kind;
			await route.fulfill({ response: answer, json: body });
		});
		await page.goto('/datasets/lake~acme%24letters');
		await page.getByRole('button', { name: 'Grid', exact: true }).click();

		const tiles = page.getByRole('list', { name: 'Documents' }).getByRole('listitem');
		await expect(tiles).toHaveCount(3);
		if (kind === 'audio') {
			await expect(tiles.getByRole('img', { name: 'A recording' })).toHaveCount(3);
		} else {
			const image = tiles.first().locator('img');
			await expect
				.poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth))
				.toBeGreaterThan(0);
		}
	});
}

test('a dock narrowed past the breakpoint stacks its panels, and widened again shows the person’s own', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/datasets/lake~acme%24letters');
	const search = page.getByRole('searchbox');
	const player = page.getByRole('region', { name: 'Player' });
	await expect(player).toBeVisible();
	/** Whether the Player sits beside the Results (wide) rather than under them (stacked); null while either is
	 *  not laid out (the dock remounts its panels as it rearranges them). */
	const beside = async (): Promise<boolean | null> => {
		const [p, s] = [await player.boundingBox(), await search.boundingBox()];
		return p && s ? p.x > s.x + 100 : null;
	};
	await expect.poll(beside).toBe(true);

	await page.setViewportSize({ width: 390, height: 844 });
	await expect.poll(beside).toBe(false);
	const stacked = (await player.boundingBox())!;
	expect(stacked.y).toBeGreaterThan((await search.boundingBox())!.y);

	await page.setViewportSize({ width: 1440, height: 900 });
	await expect.poll(beside).toBe(true);
});

test('the hits are a list, a grid or a table, and a still-image dataset offers no clip to play', async ({
	page,
	context,
}) => {
	await signIn(context, person('Grace'));
	await page.goto('/datasets/lake~acme%24letters');
	await searchFor(page, 'harbour');
	await expect(results(page).getByRole('listitem')).toHaveCount(3);

	await page.getByRole('button', { name: 'Grid', exact: true }).click();
	await expect(results(page).getByRole('listitem')).toHaveCount(3);
	await expect(page.getByRole('button', { name: 'Play clip' })).toHaveCount(0);

	await page.getByRole('button', { name: 'Table', exact: true }).click();
	const table = page.getByRole('table');
	await expect(table.getByRole('columnheader', { name: 'Collection' })).toBeVisible();
	await expect(table.getByRole('columnheader', { name: 'Play' })).toHaveCount(0);
	await expect(table.getByRole('cell', { name: 'Maps' })).toBeVisible();
});

test('a saved search is kept for the person and applied again on another visit', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	await page.goto('/datasets/lake~acme%24letters');
	await searchFor(page, 'harvest');
	await expect(results(page).getByRole('listitem')).toHaveCount(1);

	await page.getByRole('button', { name: 'Saved searches', exact: true }).click();
	await page.getByLabel('Name of the search to save').fill('Harvest letters');
	await page.getByRole('button', { name: 'Save search' }).click();
	await page.keyboard.press('Escape');

	// A visit with nothing searched (the dataset's own address, not this search's link).
	await page.goto('/datasets/lake~acme%24letters');
	await expect(page.getByText('Browsing 3 documents')).toBeVisible();
	// The trigger counts the searches kept for this dataset.
	await page.getByRole('button', { name: /^Saved searches\s*1$/ }).click();
	await page.getByRole('button', { name: 'Harvest letters', exact: true }).click();

	await expect(results(page).getByRole('listitem')).toHaveCount(1);
	await expect(page.getByRole('searchbox')).toHaveValue('harvest');
});

test('a topic link opens the page filtered to it, and the keyboard reaches the search and the player', async ({
	page,
	context,
}) => {
	await signIn(context, person('Margaret'));
	await page.goto('/datasets/lake~acme%24letters?topic=Maps');

	await expect(results(page).getByRole('listitem')).toHaveCount(1);
	await results(page)
		.getByRole('button', { name: /Soundings along the harbour/ })
		.click();
	const player = page.getByRole('region', { name: 'Player' });
	await expect(player.getByRole('heading', { name: 'A map of the harbour' })).toBeVisible();

	await page.keyboard.press('Escape');
	await expect(player.getByText('Click a hit to play.')).toBeVisible();
	await page.keyboard.press('/');
	await expect(page.getByRole('searchbox')).toBeFocused();

	// `/` is the dataset's search, not the first search box on the page: with the graph in front and the
	// results closed, it brings the results back and focuses their search.
	await page.getByRole('tab', { name: /^Graph/ }).click();
	await page.getByRole('button', { name: 'Close Results' }).click();
	await page.getByRole('combobox', { name: 'Search entities' }).blur();
	await page.keyboard.press('/');
	await expect(page.getByRole('searchbox', { name: /this dataset/ })).toBeFocused();
});

test('a failed search, a failed browse, a dataset with nothing and a missing dataset each say so', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets/lake~acme%24letters');
	await searchFor(page, 'fail');
	await expect(page.getByRole('alert')).toHaveText(
		'Search failed: the search index could not be read',
	);
	await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();

	await page.goto('/datasets/lake~acme%24notes');
	await expect(page.getByText('This dataset declares no search')).toBeVisible();
	await expect(page.getByText('This dataset has no documents to browse.')).toBeVisible();
	await expect(page.getByRole('searchbox')).toHaveCount(0);

	await page.goto('/datasets/lake~acme%24nope');
	await expect(page.getByRole('alert')).toContainText('No dataset lake~acme$nope that you can see');
	await expect(page.getByRole('link', { name: 'All datasets' })).toBeVisible();
});

test('a browse the viewer cannot answer says why and offers to try again', async ({
	page,
	context,
}) => {
	await signIn(context, person('Broken'));
	await page.goto('/datasets/lake~acme%24letters');
	await expect(page.getByRole('alert')).toHaveText(
		'The documents could not be read: the documents table could not be opened',
	);
	await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
});

test('a filter narrows the results, shows as a pill, and clearing it brings them back', async ({
	page,
	context,
}) => {
	await signIn(context, person('Grace'));
	await page.goto('/datasets/lake~acme%24letters');
	await searchFor(page, 'harbour');
	await expect(results(page).getByRole('listitem')).toHaveCount(3);

	await page.getByRole('button', { name: 'Filters' }).click();
	await page.getByRole('button', { name: 'Column', exact: true }).click();
	await page.getByRole('option', { name: /collection/ }).click();
	await page.getByPlaceholder('value…').fill('Maps');
	await page.getByRole('button', { name: 'Add', exact: true }).click();
	await page.keyboard.press('Escape');

	await expect(results(page).getByRole('listitem')).toHaveCount(1);
	await expect(results(page)).toContainText('Soundings along the harbour mouth.');
	await page.getByRole('button', { name: /^Remove .* filter$/ }).click();
	await expect(results(page).getByRole('listitem')).toHaveCount(3);
});

test('a thumbnail whose signed link expired is signed again and shown, not hidden', async ({
	page,
	context,
}) => {
	const who = person('Ada');
	await signIn(context, who);
	// This person's links serve two seconds (the viewer allows ten and up; the stand-in shortens it so
	// the spec waits in real time: a faked browser clock would date every DPoP proof ahead of the
	// services' and be refused).
	await page.request.post(`${EDGE}/api/viewer/__ttl?sub=${encodeURIComponent(who.sub)}&seconds=2`);
	// The first loads of every image fail, so no expired link is in the document's image cache, which
	// serves a URL it holds whatever its link's life.
	await page.route('**/api/viewer/api/signed-media/*', (route) => route.abort());
	await page.goto('/datasets/lake~acme%24letters');
	await searchFor(page, 'harbour');
	await page.getByRole('button', { name: 'Grid', exact: true }).click();
	await expect(results(page).getByRole('listitem')).toHaveCount(3);
	await page.unroute('**/api/viewer/api/signed-media/*');

	// Every link signed so far has expired; in another layout the cards mount again and ask for them.
	// The renewal answers a second late, as a busy viewer's may, so an expired link handed out would
	// fail before its replacement arrives.
	await page.waitForTimeout(2500);
	await page.route('**/api/viewer/api/signed-media', async (route) => {
		await new Promise((done) => setTimeout(done, 1000));
		await route.continue();
	});
	await page.getByRole('button', { name: 'List', exact: true }).click();

	// Each of the three cards shows its thumbnail and its chunk's frame, every one an image again.
	const thumbnails = results(page).locator('img');
	await expect(thumbnails).toHaveCount(6);
	for (const thumbnail of await thumbnails.all()) {
		await expect(thumbnail).toBeVisible();
		await expect
			.poll(() => thumbnail.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth))
			.toBeGreaterThan(0);
	}
});

test('dark mode is readable: a search match on its highlight and the Search button each meet 4.5:1', async ({
	page,
	context,
}) => {
	await signIn(context, person('Margaret'));
	await page.addInitScript(() =>
		localStorage.setItem(
			'workbench-appearance',
			JSON.stringify({ v: 2, theme: 'ink', mode: 'dark', accent: null }),
		),
	);
	await page.goto('/datasets/lake~acme%24letters');
	await searchFor(page, 'harbour');
	const match = results(page).locator('mark').first();
	await expect(match).toBeVisible();

	const ratios = [
		await textContrast(match),
		await textContrast(page.getByRole('button', { name: 'Search', exact: true })),
	];
	expect(
		ratios.every((ratio) => ratio >= 4.5),
		`contrast ${ratios.map((r) => r.toFixed(2))}`,
	).toBe(true);
});

test('a dataset opened while another one is still loading is the one the page reads', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	// The notes' descriptor answers late: by then the reader has gone on to the letters.
	let released: () => void = () => {};
	const late = new Promise<void>((done) => (released = done));
	await page.route('**/api/viewer/api/datasets/lake~acme%24notes/descriptor', async (route) => {
		await late;
		await route.continue();
	});
	await page.goto('/datasets');
	const datasets = page.getByRole('list', { name: 'Datasets in The lakehouse' });
	await datasets.getByRole('link', { name: /acme\$notes/ }).click();
	await datasets.getByRole('link', { name: /acme\$letters/ }).click();
	await expect(page.getByText('Browsing 3 documents')).toBeVisible();

	const landed = page.waitForResponse('**/lake~acme%24notes/descriptor');
	released();
	await landed;
	await page.waitForTimeout(100);
	const browse = page.waitForRequest((req) => req.url().includes('/api/documents'));
	await page.getByRole('button', { name: 'Refresh' }).click();
	expect(new URL((await browse).url()).searchParams.get('dataset')).toBe('lake~acme$letters');
});
