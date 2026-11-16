import { expect, test, type Locator, type Page } from '@playwright/test';
import { callsOf, person, signIn, watchCsp } from './session';

// The atlas and the graph drawn by their WebGPU renderers, on Chromium's software adapter
// (SwiftShader, `--enable-unsafe-webgpu`; the `webgpu` project in `playwright.config.ts`), under the
// app's real CSP header.

/** How many pixels in the middle of a canvas carry colour: the points and nodes are coloured, the
 *  cleared background and its dot grid are grey, and the overlays sit at the edges. Read from a screenshot,
 *  since a WebGPU canvas's frame is not the page's to read back. */
async function drawnPixels(page: Page, canvas: Locator): Promise<number> {
	await expect(canvas).toBeVisible();
	const png = await canvas.screenshot();
	return page.evaluate(
		async (bytes) => {
			const bitmap = await createImageBitmap(
				new Blob([new Uint8Array(bytes)], { type: 'image/png' }),
			);
			const [w, h] = [Math.floor(bitmap.width / 2), Math.floor(bitmap.height / 2)];
			const scratch = new OffscreenCanvas(w, h);
			const context = scratch.getContext('2d');
			if (!context) return 0;
			context.drawImage(bitmap, -bitmap.width / 4, -bitmap.height / 4);
			const { data } = context.getImageData(0, 0, w, h);
			let drawn = 0;
			for (let i = 0; i < data.length; i += 4) {
				const rgb = [data[i] ?? 0, data[i + 1] ?? 0, data[i + 2] ?? 0];
				if (Math.max(...rgb) - Math.min(...rgb) > 60) drawn += 1;
			}
			return drawn;
		},
		[...png],
	);
}

test('the atlas draws its points under the CSP, revalidates them by ETag, and a cluster lists its rows', async ({
	page,
	context,
}) => {
	const who = person('Grace');
	await signIn(context, who);
	const violations = await watchCsp(page);
	await page.goto('/datasets/acme%24letters/atlas');

	await expect(page.getByText('60 pts')).toBeVisible();
	const canvas = page.locator('canvas').first();
	await expect.poll(() => drawnPixels(page, canvas)).toBeGreaterThan(100);

	// A legend's cluster is a selection: its rows are listed, and one opens in the player.
	await page.getByRole('button', { name: 'Select cluster #0, 20 points' }).click();
	await expect(page.getByRole('status').filter({ hasText: '20 rows' })).toBeVisible();
	const body = page.getByRole('table').getByRole('rowgroup').nth(1);
	await expect(body.getByRole('row')).toHaveCount(20);
	// The keyboard opens a row as a click does.
	await body.getByRole('row').first().focus();
	await page.keyboard.press('Enter');
	await expect(
		page
			.getByRole('region', { name: 'Player' })
			.getByRole('img', { name: 'A letter from the harbour' }),
	).toBeVisible();

	// A second visit revalidates the points the browser holds: the viewer runs the read again and answers
	// 304, and no bytes travel. A reload signs in again (the tokens were in memory) and asks.
	await page.reload();
	await expect(page.getByText('60 pts')).toBeVisible();
	await expect
		.poll(async () =>
			(await callsOf(page, who))
				.filter((call) => call.path === '/api/atlas/points')
				.map((call) => call.status),
		)
		.toEqual([200, 304]);
	expect((await callsOf(page, who)).filter((call) => call.refused !== undefined)).toEqual([]);
	expect(await violations()).toEqual([]);
});

/** How many coloured pixels a page screenshot holds inside `clip`: the points are coloured, the background grey. */
async function colouredIn(
	page: Page,
	clip: { x: number; y: number; width: number; height: number },
): Promise<number> {
	const png = await page.screenshot({ clip });
	return page.evaluate(
		async (bytes) => {
			const bitmap = await createImageBitmap(
				new Blob([new Uint8Array(bytes)], { type: 'image/png' }),
			);
			const scratch = new OffscreenCanvas(bitmap.width, bitmap.height);
			const context = scratch.getContext('2d');
			if (!context) return -1;
			context.drawImage(bitmap, 0, 0);
			const { data } = context.getImageData(0, 0, bitmap.width, bitmap.height);
			let coloured = 0;
			for (let i = 0; i < data.length; i += 4) {
				const rgb = [data[i] ?? 0, data[i + 1] ?? 0, data[i + 2] ?? 0];
				if (Math.max(...rgb) - Math.min(...rgb) > 60) coloured += 1;
			}
			return coloured;
		},
		[...png],
	);
}

test('the atlas fits its points in the area its toolbar and status bar leave free', async ({
	page,
	context,
}) => {
	await signIn(context, person('Grace'));
	await page.goto('/datasets/acme%24letters/atlas');
	await expect(page.getByText('60 pts')).toBeVisible();
	const canvas = page.locator('canvas').first();
	await expect.poll(() => drawnPixels(page, canvas)).toBeGreaterThan(100);

	const map = (await canvas.boundingBox())!;
	const toolbar = page.getByText('60 pts').locator('..');
	const status = page.getByText('drag to lasso', { exact: false }).locator('..');
	const top = (await toolbar.boundingBox())!;
	const bottom = (await status.boundingBox())!;
	// The overlays hidden, the areas they covered show the map alone: no point was drawn there.
	await toolbar.evaluate((el) => (el.style.visibility = 'hidden'));
	await status.evaluate((el) => (el.style.visibility = 'hidden'));
	const above = { x: top.x, y: map.y, width: top.width, height: top.y + top.height - map.y };
	const below = {
		x: bottom.x,
		y: bottom.y,
		width: bottom.width,
		height: map.y + map.height - bottom.y,
	};
	expect([await colouredIn(page, above), await colouredIn(page, below)]).toEqual([0, 0]);
});

test('in the dock, a map selection becomes the results, and a search afterwards replaces it', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets/acme%24letters');
	await expect(page.getByText('60 pts')).toBeVisible();
	await expect(page.getByText('Browsing 3 documents')).toBeVisible();

	// The panel is narrower than the map's toolbar and legend together, so the legend is a toggle.
	const legend = page.getByRole('button', { name: 'Legend' });
	await expect(legend).toHaveAttribute('aria-pressed', 'false');
	await legend.click();
	await page.getByRole('button', { name: 'Select cluster #1, 20 points' }).click();
	await page.getByRole('button', { name: 'Show as results' }).click();
	const results = page.getByRole('list', { name: 'Search results' }).getByRole('listitem');
	await expect(page.getByText('Atlas selection: 20 points')).toBeVisible();
	await expect(results).toHaveCount(20);

	await page.getByRole('button', { name: 'Clear atlas selection' }).click();
	await expect(page.getByText('Browsing 3 documents')).toBeVisible();

	await page.getByRole('searchbox').fill('harbour');
	await page.getByRole('button', { name: 'Search', exact: true }).click();
	await expect(results).toHaveCount(3);
	await expect(page.getByText('Atlas selection: 20 points')).toHaveCount(0);
});

test('the graph draws its entities under the CSP, and a node’s panel re-centres it', async ({
	page,
	context,
}) => {
	await signIn(context, person('Barbara'));
	const violations = await watchCsp(page);
	await page.goto('/datasets/acme%24letters/graph');

	const canvas = page.locator('canvas').first();
	await expect.poll(() => drawnPixels(page, canvas)).toBeGreaterThan(100);

	await page.getByRole('searchbox', { name: 'Search entities' }).fill('fleet');
	await page.getByRole('button', { name: /The fleet/ }).click();
	const panel = page.getByRole('complementary', { name: 'The fleet' });
	await panel.getByRole('button', { name: 'to The harbour', exact: true }).click();
	await expect(page.getByRole('complementary', { name: 'The harbour', exact: true })).toBeVisible();
	await expect(page.getByRole('navigation', { name: 'Graph trail' })).toContainText('The harbour');
	expect(await violations()).toEqual([]);
});

test('another dataset’s atlas draws that dataset’s points, read for it', async ({
	page,
	context,
}) => {
	const who = person('Margaret');
	await signIn(context, who);
	await page.goto('/datasets/acme%24letters/atlas');
	await expect(page.getByText('60 pts')).toBeVisible();

	// Straight from one dataset's atlas to another's, client-side, inside the dataset layout.
	await gotoInPage(page, '/datasets/acme%24maps/atlas');
	await expect(page.getByText('24 pts')).toBeVisible();
	const points = (await callsOf(page, who)).filter((c) => c.path === '/api/atlas/points');
	expect(points.at(-1)?.query).toContain('dataset=acme%24maps');
});

test('a cluster hidden on the map keeps its legend row legible (WCAG 2.2 1.4.3)', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets/acme%24letters/atlas');
	const hide = page.getByRole('button', { name: 'Hide cluster #0 on the map' });
	await hide.click();
	await expect(hide).toHaveAttribute('aria-pressed', 'true');
	const label = page.getByRole('button', { name: /^Select cluster #0, / }).getByText('#0');
	await expect(label).toHaveCSS('text-decoration-line', 'line-through');
	expect(await contrastOf(label)).toBeGreaterThanOrEqual(4.5);
});

/** The contrast ratio of an element's text against the backgrounds painted under it, its own and its
 *  ancestors' opacity applied. Colours are converted by the browser's 2-D canvas, so any CSS colour reads. */
function contrastOf(target: Locator): Promise<number> {
	return target.evaluate((el) => {
		const probe = document.createElement('canvas').getContext('2d', { willReadFrequently: true })!;
		const rgba = (css: string): number[] => {
			probe.clearRect(0, 0, 1, 1);
			probe.fillStyle = '#000';
			probe.fillStyle = css;
			probe.fillRect(0, 0, 1, 1);
			const [r = 0, g = 0, b = 0, a = 0] = probe.getImageData(0, 0, 1, 1).data;
			return [r, g, b, a / 255];
		};
		const over = (top: number[], under: number[]): number[] =>
			[0, 1, 2].map((i) => (top[i] ?? 0) * (top[3] ?? 1) + (under[i] ?? 0) * (1 - (top[3] ?? 1)));
		const lum = (c: number[]): number => {
			const f = (v: number): number =>
				(v /= 255) <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
			return 0.2126 * f(c[0] ?? 0) + 0.7152 * f(c[1] ?? 0) + 0.0722 * f(c[2] ?? 0);
		};
		const layers: number[][] = [];
		let alpha = 1;
		for (let n: Element | null = el; n; n = n.parentElement) {
			const style = getComputedStyle(n);
			alpha *= Number(style.opacity);
			const c = rgba(style.backgroundColor);
			if ((c[3] ?? 0) > 0) layers.push(c);
			if ((c[3] ?? 0) >= 1) break;
		}
		let background = [255, 255, 255];
		for (const layer of layers.reverse()) background = over(layer, background);
		const ink = rgba(getComputedStyle(el).color);
		const text = over([ink[0] ?? 0, ink[1] ?? 0, ink[2] ?? 0, (ink[3] ?? 1) * alpha], background);
		const [a, b] = [lum(text), lum(background)];
		return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
	});
}

test('in the light theme the graph clears to the light background', async ({ page, context }) => {
	await signIn(context, person('Barbara'));
	await page.addInitScript(() => localStorage.setItem('mode-watcher-mode', 'light'));
	await page.goto('/datasets/acme%24letters/graph');
	const canvas = page.locator('canvas').first();
	await expect.poll(() => drawnPixels(page, canvas)).toBeGreaterThan(100);
	// The canvas's corner, clear of nodes and overlays, is the page's light `--background`.
	const corner = await page.evaluate(
		async (bytes) => {
			const bitmap = await createImageBitmap(
				new Blob([new Uint8Array(bytes)], { type: 'image/png' }),
			);
			const scratch = new OffscreenCanvas(1, 1);
			scratch.getContext('2d')!.drawImage(bitmap, -2, -2);
			return Array.from(scratch.getContext('2d')!.getImageData(0, 0, 1, 1).data.slice(0, 3));
		},
		[...(await canvas.screenshot())],
	);
	expect(Math.min(...corner)).toBeGreaterThan(230);
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

test('an atlas selection is sent to a project whole, each item from the dataset and keyed as its row', async ({
	page,
	context,
}) => {
	const who = person('Annotator');
	await signIn(context, who);
	await page.goto('/datasets/acme%24letters/atlas?project=letters');
	await expect(page.getByText('60 pts')).toBeVisible();
	await page.getByRole('button', { name: 'Select cluster #0, 20 points' }).click();
	await expect(page.getByRole('status').filter({ hasText: '20 rows' })).toBeVisible();
	await page.getByRole('button', { name: 'Send to a project' }).click();
	const dialog = page.getByTestId('send-to-project');
	await expect(dialog.getByTestId('send-project')).toHaveValue('letters');
	const posted = page.waitForRequest((req) =>
		req.url().endsWith('/api/annotator/projects/letters/items'),
	);

	await dialog.getByRole('button', { name: /^Send \d+ items$/ }).click();

	const items = (
		(await posted).postDataJSON() as { items: { source: { where: string; keys: string[] } }[] }
	).items;
	expect(items.length).toBeGreaterThan(0);
	expect(
		items.every(
			(item) =>
				item.source.where === 'acme$letters' && /^[^/]+\/\d+$/.test(item.source.keys[0] ?? ''),
		),
	).toBe(true);
	await expect(dialog.getByTestId('send-done')).toContainText(`Sent ${items.length} items`);
	expect((await callsOf(page, who)).filter((call) => call.refused !== undefined)).toEqual([]);
});
