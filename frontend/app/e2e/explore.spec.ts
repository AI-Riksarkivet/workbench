import { expect, test, type Page } from '@playwright/test';
import { callsOf, person, signIn } from './session';
import { EDGE } from './ports';

// The explorer's features on the dataset page, on the built app: browsing before a search, the three
// layouts, the voice search, saved views, a topic link, the keyboard, and the states a reader meets
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
	await page.goto('/datasets/acme%24letters');

	const documents = page.getByRole('list', { name: 'Documents' });
	await expect(page.getByText('Browsing 3 documents')).toBeVisible();
	await expect(documents.getByRole('listitem')).toHaveCount(3);
	await documents.getByRole('button', { name: /A map of the harbour/ }).click();

	const player = page.getByRole('region', { name: 'Player' });
	await expect(player.getByRole('img', { name: 'A map of the harbour' })).toBeVisible();
});

test('the hits are a list, a grid or a table, and a still-image dataset offers no clip to play', async ({
	page,
	context,
}) => {
	await signIn(context, person('Grace'));
	await page.goto('/datasets/acme%24letters');
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

test('a voice search finds the speaker elsewhere, and clearing it returns to the text results', async ({
	page,
	context,
}) => {
	const who = person('Barbara');
	await signIn(context, who);
	await page.goto('/datasets/acme%24letters');
	await searchFor(page, 'master');
	await expect(results(page).getByRole('listitem')).toHaveCount(1);

	await results(page).getByRole('button', { name: 'Find this voice' }).click();

	await expect(page.getByText(/Voice: A letter from the harbour/)).toBeVisible();
	await expect(results(page).getByRole('listitem')).toHaveCount(2);
	await page.getByRole('button', { name: 'Clear voice search' }).click();
	await expect(results(page).getByRole('listitem')).toHaveCount(1);
	const calls = await callsOf(page, who);
	expect(calls).toContainEqual({
		service: 'viewer',
		method: 'GET',
		path: '/api/voice/similar',
		status: 200,
	});
});

test('a saved search is kept for the person and applied again after a reload', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	await page.goto('/datasets/acme%24letters');
	await searchFor(page, 'harvest');
	await expect(results(page).getByRole('listitem')).toHaveCount(1);

	await page.getByRole('button', { name: 'Saved searches', exact: true }).click();
	await page.getByLabel('Name of the search to save').fill('Harvest letters');
	await page.getByRole('button', { name: 'Save search' }).click();
	await page.keyboard.press('Escape');

	await page.reload();
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
	await page.goto('/datasets/acme%24letters?topic=Maps');

	await expect(results(page).getByRole('listitem')).toHaveCount(1);
	await results(page)
		.getByRole('button', { name: /Soundings along the harbour/ })
		.click();
	const player = page.getByRole('region', { name: 'Player' });
	await expect(player.getByRole('img')).toBeVisible();

	await page.keyboard.press('Escape');
	await expect(player.getByText('Click a hit to play.')).toBeVisible();
	await page.keyboard.press('/');
	await expect(page.getByRole('searchbox')).toBeFocused();
});

test('a failed search, a failed browse, a dataset with nothing and a missing dataset each say so', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets/acme%24letters');
	await searchFor(page, 'fail');
	await expect(page.getByRole('alert')).toHaveText(
		'Search failed: the search index could not be read',
	);
	await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();

	await page.goto('/datasets/acme%24notes');
	await expect(page.getByText('This dataset declares no search')).toBeVisible();
	await expect(page.getByText('This dataset has no documents to browse.')).toBeVisible();
	await expect(page.getByRole('searchbox')).toHaveCount(0);

	await page.goto('/datasets/acme%24nope');
	await expect(page.getByRole('alert')).toContainText('No dataset acme$nope that you can see');
	await expect(page.getByRole('link', { name: 'All datasets' })).toBeVisible();
});

test('a browse the viewer cannot answer says why and offers to try again', async ({
	page,
	context,
}) => {
	await signIn(context, person('Broken'));
	await page.goto('/datasets/acme%24letters');
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
	await page.goto('/datasets/acme%24letters');
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
	await page.goto('/datasets/acme%24letters');
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

test('a search match is readable in dark mode: its text on its highlight meets 4.5:1', async ({
	page,
	context,
}) => {
	await signIn(context, person('Margaret'));
	await page.addInitScript(() => localStorage.setItem('mode-watcher-mode', 'dark'));
	await page.goto('/datasets/acme%24letters');
	await searchFor(page, 'harbour');
	const match = results(page).locator('mark').first();
	await expect(match).toBeVisible();

	// Every colour resolved by the browser itself (a canvas paints the computed CSS colour), the
	// highlight composited over the opaque surface behind it, then WCAG 2.2's contrast ratio.
	const ratio = await match.evaluate((mark: HTMLElement) => {
		const context = document
			.createElement('canvas')
			.getContext('2d', { willReadFrequently: true })!;
		const rgba = (css: string): [number, number, number, number] => {
			context.clearRect(0, 0, 1, 1);
			context.fillStyle = css;
			context.fillRect(0, 0, 1, 1);
			const [r = 0, g = 0, b = 0, a = 0] = context.getImageData(0, 0, 1, 1).data;
			return [r, g, b, a / 255];
		};
		let surface: HTMLElement | null = mark.parentElement;
		let behind = rgba('white');
		while (surface) {
			const color = rgba(getComputedStyle(surface).backgroundColor);
			if (color[3] === 1) {
				behind = color;
				break;
			}
			surface = surface.parentElement;
		}
		const [hr, hg, hb, ha] = rgba(getComputedStyle(mark).backgroundColor);
		const over = (front: number, back: number): number => front * ha + back * (1 - ha);
		const background = [over(hr, behind[0]), over(hg, behind[1]), over(hb, behind[2])];
		const text = rgba(getComputedStyle(mark).color).slice(0, 3);
		const luminance = (rgb: number[]): number => {
			const [r = 0, g = 0, b = 0] = rgb.map((c) => {
				const s = c / 255;
				return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
			});
			return 0.2126 * r + 0.7152 * g + 0.0722 * b;
		};
		const [hi, lo] = [luminance(text), luminance(background)].sort((x, y) => y - x);
		return ((hi ?? 0) + 0.05) / ((lo ?? 0) + 0.05);
	});
	expect(ratio).toBeGreaterThanOrEqual(4.5);
});

test('a dataset opened while another one is still loading is the one the page reads', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	// The notes' descriptor answers late: by then the reader has gone on to the letters.
	let released: () => void = () => {};
	const late = new Promise<void>((done) => (released = done));
	await page.route('**/api/viewer/api/datasets/acme%24notes/descriptor', async (route) => {
		await late;
		await route.continue();
	});
	await page.goto('/datasets');
	const datasets = page.getByRole('list', { name: 'Datasets' });
	await datasets.getByRole('link', { name: /acme\$notes/ }).click();
	await datasets.getByRole('link', { name: /acme\$letters/ }).click();
	await expect(page.getByText('Browsing 3 documents')).toBeVisible();

	const landed = page.waitForResponse('**/acme%24notes/descriptor');
	released();
	await landed;
	await page.waitForTimeout(100);
	const browse = page.waitForRequest((req) => req.url().includes('/api/documents'));
	await page.getByRole('button', { name: 'Refresh' }).click();
	expect(new URL((await browse).url()).searchParams.get('dataset')).toBe('acme$letters');
});
