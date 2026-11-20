import {
	expect,
	test,
	type BrowserContext,
	type Locator,
	type Page,
	type Request,
} from '@playwright/test';
import { AFTER_THE_WAIT_MS, adapterAnswersLate } from './adapter';
import { callsOf, person, signIn, watchCsp } from './session';
import { EDGE } from './ports';
import type { MockIdentity } from './mock-oidc';

// The labeling canvas on the built app, drawn by pixi's WebGPU renderer on Chromium's software adapter (the `webgpu`
// project in `playwright.config.ts`) under the app's real CSP. The item is `pages-t1` of the seeded `pages` project
// (`e2e/mock-projects.ts`), claimed by the reader: a 160 × 160 picture signed by the viewer, with a figure box top left
// and a caption box bottom right (`e2e/mock-annotations.ts`). The suite's config sets the autosave an hour off
// (`e2e/app-config.json`), so a test's own save is the only one.

const SIZE = 160;
const UNIT = 'letter-001/0';
const FIGURE = { x: 35, y: 30 };
const CAPTION = { x: 120, y: 120 };
/** The caption's box, a little larger, so its outline is inside. */
const CAPTION_BOX = { x: 86, y: 96, width: 68, height: 48 };
/** The item's name, read off its row and its dataset's declaration: its document's title. */
const NAME = 'A letter from the harbour · page 1';
const CANVAS = `Labeling canvas: ${NAME}`;

async function as(context: BrowserContext, name: string): Promise<MockIdentity> {
	const who = person(name);
	await signIn(context, who);
	return who;
}

/** Wait until the canvas says it takes input (`data-ready`, `aria-busy` off): the picture fitted, the annotations drawn,
 *  the controller attached, and no page transition covering it, during which every press goes to the document root. */
async function ready(page: Page): Promise<void> {
	await expect(page.getByTestId('canvas-region')).toHaveAttribute('data-ready', 'true');
}

/** Open the item's labeling page and wait for the canvas to be ready, and, beside it, for the filmstrip the queue's
 *  read fills: the canvas refits the picture when the strip takes its width, and a click aimed before that lands
 *  beside the shape. */
async function openItem(page: Page, { strip = true } = {}): Promise<void> {
	await page.goto('/projects/pages/tasks/pages-t1?project=pages');
	await ready(page);
	if (strip)
		await expect(
			page.getByRole('navigation', { name: 'Your claimed items' }).getByRole('link'),
		).toHaveCount(2);
}

/** Plant a fault on one service path for one person (`mock-services.ts`'s `/__fault`). */
async function fault(page: Page, who: MockIdentity, path: string, kind: string): Promise<void> {
	const query = new URLSearchParams({ sub: who.sub, path, kind });
	await page.request.post(`${EDGE}/api/viewer/__fault?${query}`);
}

/** Wait until the picture is fitted to the box the canvas is laid out in now (`data-fitted`): the filmstrip and the
 *  inspector settle after the first fit, and a point read off the box before the refit lands beside the picture. */
async function fitted(page: Page): Promise<void> {
	await expect
		.poll(() =>
			page.getByTestId('pixi-canvas').evaluate((el) => {
				const [w, h] = (el.getAttribute('data-fitted') ?? '').split('x').map(Number);
				const box = el.getBoundingClientRect();
				return Math.abs((w ?? NaN) - box.width) < 1 && Math.abs((h ?? NaN) - box.height) < 1;
			}),
		)
		.toBe(true);
}

/** Where an image point is on the page: the picture is fitted to the canvas and centred. */
async function onScreen(
	page: Page,
	at: { x: number; y: number },
): Promise<{ x: number; y: number }> {
	await ready(page);
	await fitted(page);
	const box = await page.locator('canvas').first().boundingBox();
	if (!box) throw new Error('the canvas has no box: the engine never rendered');
	const scale = Math.min(box.width / SIZE, box.height / SIZE);
	return {
		x: box.x + (box.width - SIZE * scale) / 2 + at.x * scale,
		y: box.y + (box.height - SIZE * scale) / 2 + at.y * scale,
	};
}

async function clickImage(page: Page, at: { x: number; y: number }): Promise<void> {
	const p = await onScreen(page, at);
	await page.mouse.click(p.x, p.y);
}

/** Drag a box with the rectangle tool, between two image points. */
async function drawBox(
	page: Page,
	from: { x: number; y: number },
	to: { x: number; y: number },
): Promise<void> {
	await page.keyboard.press('b');
	const a = await onScreen(page, from);
	const b = await onScreen(page, to);
	await page.mouse.move(a.x, a.y);
	await page.mouse.down();
	await page.mouse.move(b.x, b.y, { steps: 4 });
	await page.mouse.up();
	// The class picker opens on the drawn box; the task's first class.
	await page.getByTestId('class-picker').getByRole('option').first().click();
}

/** The canvas's pixels over an image rectangle, as the renderer drew them (a WebGPU canvas's frame is read from a
 *  screenshot), with the pointer off the canvas so no hover highlight is among them. The rectangle keeps the overlays
 *  drawn over the canvas (the status, the zoom) out of the comparison. */
async function pixels(page: Page, rect = CAPTION_BOX): Promise<Buffer> {
	await page.mouse.move(0, 0);
	const from = await onScreen(page, { x: rect.x, y: rect.y });
	const to = await onScreen(page, { x: rect.x + rect.width, y: rect.y + rect.height });
	return page.screenshot({
		clip: { x: from.x, y: from.y, width: to.x - from.x, height: to.y - from.y },
	});
}

/** The pixels once the renderer has settled: two reads in a row that match. */
async function settledPixels(page: Page, rect = CAPTION_BOX): Promise<Buffer> {
	let last = await pixels(page, rect);
	await expect
		.poll(async () => {
			const next = await pixels(page, rect);
			const same = Buffer.compare(next, last) === 0;
			last = next;
			return same;
		})
		.toBe(true);
	return last;
}

/** Every annotations call the page makes, as the browser sent it. */
function annotationCalls(page: Page): Request[] {
	const seen: Request[] = [];
	page.on('request', (req) => {
		if (new URL(req.url()).pathname.startsWith('/api/annotator/api/annotations/')) seen.push(req);
	});
	return seen;
}

/** The requests the page sends to ``path`` from now on. */
function calls(page: Page, path: string): Request[] {
	const seen: Request[] = [];
	page.on('request', (req) => {
		if (req.url().includes(path)) seen.push(req);
	});
	return seen;
}

const saved = (page: Page) =>
	page.waitForRequest(
		(req) => req.method() === 'POST' && req.url().includes(`/api/annotations/${UNIT}?`),
	);

test('an item opens on the canvas from its queue, drawn by WebGPU under the CSP, every annotations call naming its project and task', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	const violations = await watchCsp(page);
	const calls = annotationCalls(page);
	const errors: string[] = [];
	page.on('console', (message) => {
		if (message.type() === 'error') errors.push(message.text());
	});
	await page.goto('/projects/pages');

	await page.getByRole('link', { name: `Annotate ${NAME}` }).click();

	await expect(page).toHaveURL(/\/projects\/pages\/tasks\/pages-t1\?project=pages$/);
	await expect(page.getByTestId('annotate-status')).toContainText('2 annotations');
	await expect(page.getByTestId('pixi-canvas')).toHaveAttribute('data-renderer', 'webgpu');
	const list = page.getByTestId('annotation-list');
	await expect(list).toContainText('figure');
	await expect(list).toContainText('caption');
	await expect(page.getByRole('application', { name: CANVAS })).toBeVisible();
	expect(calls.length).toBeGreaterThan(0);
	for (const call of calls) {
		const url = new URL(call.url());
		expect([...url.searchParams.entries()].toSorted()).toEqual([
			['dataset', 'lake~acme$letters'],
			['project', 'pages'],
			['task', 'pages-t1'],
		]);
		expect(call.headers()['authorization']).toMatch(/^DPoP /);
	}
	expect((await callsOf(page, who)).filter((call) => call.refused !== undefined)).toEqual([]);
	expect(await violations()).toEqual([]);
	// No draft written yet is an answer, not a failure: nothing on the page logs an error.
	expect(errors).toEqual([]);
});

test('a GPU adapter that answers with none after the app started: the canvas says so in the atlas’s words, and offers the way back', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	test.slow();
	await adapterAnswersLate(page, AFTER_THE_WAIT_MS, true);
	await page.goto('/projects/pages/tasks/pages-t1?project=pages');

	const refusal = page.getByTestId('canvas-gpu-refusal');
	await expect(refusal).toContainText('WebGPU is required (no GPU adapter available)');
	await expect(refusal.getByRole('link', { name: 'Back to the project' })).toHaveAttribute(
		'href',
		'/projects/pages?project=pages',
	);
	await expect(page.locator('canvas')).toHaveCount(0);
});

test('every tool is chosen from the keyboard, by its hotkey and by its focused button, and `?` lists the keys', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	const toolbar = page.getByRole('toolbar', { name: 'Labeling tools' });
	const tools: [string, string][] = [
		['Select', 'v'],
		['Pan', 'h'],
		['Lasso', 'l'],
		['Rectangle', 'b'],
		['Polygon', 'g'],
		['Point', 'd'],
		['Line', 'n'],
		['Pencil (freehand)', 'f'],
		['Brush', 'u'],
		['Magnetic (corner-snap)', 'm'],
	];
	for (const [name, key] of tools) {
		await page.keyboard.press('Escape');
		await page.keyboard.press(key);
		await expect(toolbar.getByRole('button', { name, exact: true })).toHaveAttribute(
			'aria-pressed',
			'true',
		);
	}
	for (const [name] of tools.toReversed()) {
		const button = toolbar.getByRole('button', { name, exact: true });
		await button.focus();
		await page.keyboard.press('Enter');
		await expect(button).toHaveAttribute('aria-pressed', 'true');
	}
	// The canvas is reached by Tab, and shows that it has the focus.
	const region = page.getByRole('application', { name: CANVAS });
	await toolbar.getByRole('button', { name: 'Delete', exact: true }).focus();
	for (let i = 0; i < 20 && !(await region.evaluate((el) => el === document.activeElement)); i++)
		await page.keyboard.press('Tab');
	await expect(region).toBeFocused();
	// The focus ring is one unbroken outline over the picture: a layer above the canvas carries it, so no part of it is
	// painted under the picture.
	const ring = await region.evaluate((el) => {
		const after = getComputedStyle(el, '::after');
		return {
			shadow: after.boxShadow,
			z: after.zIndex,
			inset: [after.top, after.left, after.right, after.bottom],
		};
	});
	expect(ring.shadow).not.toBe('none');
	expect(ring).toMatchObject({ z: '10', inset: ['0px', '0px', '0px', '0px'] });

	await page.keyboard.press('?');
	const help = page.getByRole('dialog', { name: 'Canvas keys' });
	await expect(help).toContainText('Space');
	await expect(help).toContainText('Rectangle');
	await expect(help).toContainText('Ctrl+Z');
});

test('a box, a point, a polygon and a line are drawn from the keyboard alone, counted at once, saved, and read back', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	const region = page.getByRole('application', { name: CANVAS });
	await region.focus();
	await expect(page.getByTestId('canvas-cursor')).toBeVisible();
	const press = async (...keys: string[]) => {
		for (const key of keys) await page.keyboard.press(key);
	};

	// A box: Space anchors it at the cursor, the arrows stretch it, Space commits it; the class picker opens on it and
	// its number key gives it a class, which hands the keys back to the canvas.
	await press(
		'b',
		'Space',
		'Shift+ArrowRight',
		'Shift+ArrowRight',
		'Shift+ArrowDown',
		'Space',
		'1',
	);
	// A point where the cursor is.
	await press('d', 'Shift+ArrowLeft', 'Space', '1');
	// A polygon: a vertex per Space, Enter closes it.
	await press(
		'g',
		'Shift+ArrowUp',
		'Space',
		'Shift+ArrowRight',
		'Space',
		'Shift+ArrowDown',
		'Space',
		'Enter',
		'1',
	);
	// A line, held from one Space to the next.
	await press('n', 'Shift+ArrowLeft', 'Space', 'Shift+ArrowLeft', 'Shift+ArrowLeft', 'Space', '1');
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(6);
	await expect(page.getByTestId('annotate-status')).toContainText('6 annotations');

	const save = saved(page);
	await page.keyboard.press('Control+s');
	const body = (await save).postDataJSON() as { inserts: { shape_type: string }[] };
	expect(body.inserts.map((i) => i.shape_type)).toEqual([
		'bbox',
		'keypoint',
		'polygon',
		'polyline',
	]);
	await expect(page.getByTestId('canvas-notice')).toHaveText('Annotations saved');

	await page.reload();
	await expect(page.getByTestId('annotate-status')).toContainText('6 annotations');
});

test('a point and a line read back from the service are drawn as a point and a line', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	// The point's marker, and the corner of the line's bounding box, which a line leaves empty and a box would not.
	const POINT = { x: 100, y: 30 };
	const dot = { x: POINT.x - 3, y: POINT.y - 3, width: 6, height: 6 };
	const corner = { x: 139, y: 54, width: 9, height: 9 };
	const dotBefore = await settledPixels(page, dot);
	const cornerBefore = await settledPixels(page, corner);
	await page.keyboard.press('d');
	await clickImage(page, POINT);
	await page.keyboard.press('1');
	await page.keyboard.press('n');
	const from = await onScreen(page, { x: 110, y: 60 });
	const to = await onScreen(page, { x: 145, y: 25 });
	await page.mouse.move(from.x, from.y);
	await page.mouse.down();
	await page.mouse.move(to.x, to.y, { steps: 5 });
	await page.mouse.up();
	await page.keyboard.press('1');
	const save = saved(page);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	await save;

	await page.reload();
	await expect(page.getByTestId('annotate-status')).toContainText('4 annotations');
	expect(
		Buffer.compare(await settledPixels(page, dot), dotBefore),
		'the point has no marker',
	).not.toBe(0);
	expect(
		Buffer.compare(await settledPixels(page, corner), cornerBefore),
		'the line is drawn as a box',
	).toBe(0);
});

test('a box drawn with the mouse is saved and read back after a reload, and the history lists the save and travels to the one before', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	const history = page.getByTestId('version-history');
	await history.getByRole('button', { name: 'History' }).click();
	await expect(history.getByRole('button', { name: /^v\d/ })).toHaveCount(1);
	await drawBox(page, { x: 100, y: 20 }, { x: 140, y: 60 });
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(3);

	await page.getByRole('button', { name: 'Save', exact: true }).click();
	await expect(page.getByTestId('canvas-notice')).toHaveText('Annotations saved');
	// The open panel reads the history again once the save lands: the new save is the current one, and who made it.
	await expect(history.getByRole('button', { name: /^v\d/ })).toHaveText([
		/^v2.*· current.+3$/,
		/^v1.*2$/,
	]);
	await history.getByRole('button', { name: /^v1/ }).click();
	await expect(history).toContainText('vs current: +1 −0 ~0');
	await page.reload();
	await expect(page.getByTestId('annotate-status')).toContainText('3 annotations');
});

test('an edit made while a save is in flight, or while its reload is, survives it, and is the next save’s', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	let release: () => void = () => {};
	const held = new Promise<void>((resolve) => (release = resolve));
	let releaseReload: () => void = () => {};
	const reloadHeld = new Promise<void>((resolve) => (releaseReload = resolve));
	let posted = false;
	let reloading: () => void = () => {};
	const reloadAsked = new Promise<void>((resolve) => (reloading = resolve));
	await page.route(`**/api/annotations/${UNIT}?*`, async (route) => {
		if (route.request().method() === 'POST') {
			posted = true;
			await held;
		} else if (posted) {
			reloading();
			await reloadHeld;
		}
		await route.continue();
	});
	await drawBox(page, { x: 70, y: 60 }, { x: 85, y: 80 });
	const first = saved(page);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	await first;
	// The save is held at the door; meanwhile a second box.
	await drawBox(page, { x: 100, y: 60 }, { x: 115, y: 80 });
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(4);
	release();
	// The save landed and its reload of the rows is held; meanwhile a third box.
	await reloadAsked;
	await drawBox(page, { x: 130, y: 60 }, { x: 145, y: 80 });
	const reloaded = page.waitForResponse(
		(res) => res.request().method() === 'GET' && res.url().includes(`/api/annotations/${UNIT}?`),
	);
	releaseReload();
	await reloaded;

	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(5);
	await page.unroute(`**/api/annotations/${UNIT}?*`);
	const second = saved(page);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const body = (await second).postDataJSON() as { inserts: { x: number }[] };
	expect(body.inserts.map((i) => Math.round(i.x))).toEqual([100, 130]);
});

test('leaving an item saves it first, and a save that fails keeps the person on it', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await openItem(page);
	const next = page
		.getByRole('navigation', { name: 'Your labeling queue' })
		.getByRole('link', { name: 'Next item' });
	await drawBox(page, { x: 100, y: 20 }, { x: 140, y: 60 });
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(3);

	await fault(page, who, `/api/annotations/${UNIT}`, 'fail');
	const refused = page.waitForResponse(
		(res) => res.request().method() === 'POST' && res.status() === 503,
	);
	await next.click();
	await refused;
	await expect(page.getByTestId('canvas-notice')).toContainText('so you stayed on this item');
	await expect(page).toHaveURL(/\/tasks\/pages-t1\?/);

	await fault(page, who, `/api/annotations/${UNIT}`, 'clear');
	const save = saved(page);
	await next.click();
	await save;
	await expect(page).toHaveURL(/\/tasks\/pages-t2\?/);
	await page.goBack();
	await expect(page.getByTestId('annotate-status')).toContainText('3 annotations');
});

test('a submit whose save failed says so, and a later save that lands takes the refusal away', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await openItem(page);
	await drawBox(page, { x: 100, y: 20 }, { x: 140, y: 60 });
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(3);
	// The item's one proposal is kept first, so the item is offered for submitting.
	await page.getByTestId('keep-all-proposals').click();

	await fault(page, who, `/api/annotations/${UNIT}`, 'fail');
	await page.getByTestId('task-submit').click();
	await expect(page.getByTestId('task-failure')).toContainText('Not submitted:');
	await expect(page.getByTestId('canvas-notice')).toContainText('Save failed:');

	await fault(page, who, `/api/annotations/${UNIT}`, 'clear');
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	await expect(page.getByTestId('canvas-notice')).toHaveText('Annotations saved');
	await expect(page.getByTestId('task-failure')).toHaveCount(0);
});

test('a save refused because the session ended keeps the edits through signing in again', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await openItem(page);
	await drawBox(page, { x: 100, y: 20 }, { x: 140, y: 60 });
	await fault(page, who, `/api/annotations/${UNIT}`, 'unauthorized-write');

	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const notice = page.getByTestId('canvas-notice');
	await expect(notice).toContainText('Your session ended, so nothing was saved');
	await fault(page, who, `/api/annotations/${UNIT}`, 'clear');
	await page.getByTestId('session-prompt').getByRole('button', { name: 'Sign in again' }).click();

	await expect(page).toHaveURL(/\/tasks\/pages-t1\?project=pages$/);
	await expect(notice).toHaveText('Your unsaved edits are back. Save to keep them.');
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(3);
	const save = saved(page);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const body = (await save).postDataJSON() as { inserts: unknown[] };
	expect(body.inserts).toHaveLength(1);
});

test('a service that refuses the session the person just signed in again for is said, without sending them round in a loop', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await fault(page, who, '/tasks/pages-t1', 'unauthorized');
	const authorizations: string[] = [];
	page.on('request', (req) => {
		if (new URL(req.url()).pathname === '/oidc/auth') authorizations.push(req.url());
	});

	await page.goto('/projects/pages/tasks/pages-t1?project=pages');
	await expect(page.getByTestId('task-signin')).toBeVisible();
	await page.getByTestId('session-prompt').getByRole('button', { name: 'Sign in again' }).click();

	await expect(page.getByTestId('session-failed')).toContainText(
		'You signed in again, and the annotator still refuses your sign-in.',
	);
	await expect(page.getByTestId('session-prompt')).toHaveCount(0);
	await expect(page).toHaveURL(/\/tasks\/pages-t1\?project=pages$/);
	// The app's own sign-in and the one the person asked for: none after.
	expect(authorizations).toHaveLength(2);
});

test('a layer hidden is gone from the canvas, and shown again is back', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	const shown = await settledPixels(page);

	await page.getByRole('button', { name: 'Hide layer caption' }).click();
	await expect(page.getByRole('button', { name: 'Show layer caption' })).toBeVisible();
	await expect.poll(async () => Buffer.compare(await pixels(page), shown)).not.toBe(0);

	await page.getByRole('button', { name: 'Show layer caption' }).click();
	await expect(page.getByRole('button', { name: 'Hide layer caption' })).toBeVisible();
	await expect.poll(async () => Buffer.compare(await pixels(page), shown)).toBe(0);
});

test('a deleted annotation leaves the canvas at once, and its neighbour stays', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	const detail = page.getByTestId('annotation-detail');
	// The baseline is taken after the same select and deselect the delete makes, so only the delete differs.
	await clickImage(page, CAPTION);
	await expect(detail).toContainText('caption');
	await page.keyboard.press('Escape');
	const before = await settledPixels(page);

	await clickImage(page, CAPTION);
	await expect(detail).toContainText('caption');
	await page.keyboard.press('Delete');
	await expect(page.getByTestId('annotation-list')).not.toContainText('caption');
	await page.keyboard.press('Escape');
	await expect.poll(async () => Buffer.compare(await pixels(page), before)).not.toBe(0);

	// Where the caption was, the canvas offers nothing; the figure it still offers.
	await clickImage(page, CAPTION);
	await expect(detail).toHaveCount(0);
	await clickImage(page, FIGURE);
	await expect(detail).toContainText('figure');
});

test('a relation is drawn by two picks, undone and redone, and saved on its own into the task’s work', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	await page
		.getByTestId('annotation-list')
		.getByRole('button', { name: /^figure/ })
		.click();
	const rail = page.getByTestId('relations-panel');
	await rail.getByRole('button', { name: 'caption_of' }).click();
	await expect(page.getByTestId('link-hint')).toContainText('target');
	await clickImage(page, CAPTION);
	const links = page.getByRole('button', { name: 'Remove this link' });
	await expect(links).toHaveCount(1);

	await page.keyboard.press('Control+z');
	await expect(links).toHaveCount(0);
	await page.keyboard.press('Control+Shift+z');
	await expect(links).toHaveCount(1);

	// The link alone is an edit: the save carries the item's links, and nothing else changed.
	const save = saved(page);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const body = (await save).postDataJSON() as {
		links: { name: string; from_shape: string; to_shape: string }[];
		inserts: unknown[];
		edits: unknown[];
	};
	expect([body.links, body.inserts, body.edits]).toEqual([
		[{ name: 'caption_of', from_shape: 'fig-1', to_shape: 'cap-1' }],
		[],
		[],
	]);
	await expect(page.getByTestId('canvas-notice')).toHaveText('Annotations saved');

	// Reopened, the task shows its link, read back with its rows.
	await page.reload();
	await expect(page.getByTestId('annotate-status')).toContainText('2 annotations');
	await page
		.getByTestId('annotation-list')
		.getByRole('button', { name: /^figure/ })
		.click();
	await expect(page.getByRole('button', { name: 'Remove this link' })).toHaveCount(1);
});

test('the item’s choices toggle as tag rows, which ride the save', async ({ page, context }) => {
	await as(context, 'Annotator');
	await openItem(page);
	const letter = page.getByTestId('classification-bar').getByRole('button', { name: 'letter' });
	await letter.click();
	await expect(letter).toHaveAttribute('aria-pressed', 'true');
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(3);
	// The tag's inspector names its class among the item's tag classes, and offers no conversion of a geometry it has not.
	await page.getByTestId('annotation-list').getByRole('button', { name: 'letter kept' }).click();
	await expect(
		page.getByTestId('class-chips').getByRole('button', { name: /letter/, pressed: true }),
	).toBeVisible();
	await expect(page.getByRole('button', { name: 'Convert to polygon' })).toBeDisabled();

	const save = saved(page);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const body = (await save).postDataJSON() as { inserts: { shape_type: string; label: string }[] };
	expect(body.inserts.map((i) => [i.shape_type, i.label])).toEqual([['tag', 'letter']]);
});

test('the inspector offers each class its own attributes and transcription, and an attribute edit keeps the rest of the metadata', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	const list = page.getByTestId('annotation-list');
	const detail = page.getByTestId('annotation-detail');

	// The caption transcribes, and its metadata holds no object: its attribute is refused rather than written over it.
	await list.getByRole('button', { name: /^caption/ }).click();
	await expect(detail.getByTestId('transcription-field')).toBeVisible();
	await detail.getByTestId('attr-legible').check();
	await expect(page.getByTestId('canvas-notice')).toHaveText(
		"This annotation's metadata is not an object, so legible cannot be set without losing it.",
	);
	await page.getByRole('button', { name: 'Back to list' }).click();

	await list.getByRole('button', { name: /^figure/ }).click();
	await expect(detail.getByTestId('transcription-field')).toHaveCount(0);
	await detail.getByTestId('attr-damaged').check();
	const save = saved(page);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const body = (await save).postDataJSON() as { edits: { id: string; metadata?: string }[] };
	expect(body.edits).toEqual([
		{ id: 'fig-1', metadata: '{"damaged":"true","provenance":{"source":"ocr","page":3}}' },
	]);
});

test('the brush offers its size and output, and the image adjustments reset to neutral', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	await expect(page.getByTestId('brush-controls')).toHaveCount(0);
	await page.keyboard.press('u');
	await expect(page.getByTestId('brush-controls')).toContainText('20');
	const output = page.getByTestId('brush-output');
	await expect(output).toHaveText(/mask/i);
	await output.click();
	await expect(output).toHaveText(/poly/i);

	await page.getByRole('button', { name: 'Image adjustments' }).click();
	const panel = page.getByTestId('image-adjust');
	await expect(panel.getByText('1.00')).toHaveCount(3);
	const reset = page.getByTestId('image-adjust-reset');
	await expect(reset).toBeDisabled();
	// The first slider is brightness; its thumb takes the arrow keys.
	await panel.getByRole('slider').first().focus();
	await page.keyboard.press('ArrowRight');
	await expect(reset).toBeEnabled();
	await reset.click();
	await expect(panel.getByText('1.00')).toHaveCount(3);
});

test('annotations that cannot be read leave the canvas read-only, and say why', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await fault(page, who, `/api/annotations/${UNIT}`, 'fail');
	// The read is held: while it is under way the canvas is loading, and says nothing is unavailable.
	let release = (): void => {};
	const held = new Promise<void>((resolve) => (release = resolve));
	await page.route(`**/api/annotator/api/annotations/${UNIT}?*`, async (route) => {
		await held;
		await route.continue();
	});
	await page.goto('/projects/pages/tasks/pages-t1?project=pages');
	await expect(page.getByTestId('annotate-status')).toContainText('loading');
	await expect(page.getByTestId('canvas-readonly')).toHaveCount(0);
	release();
	await expect(page.getByTestId('annotate-status')).toContainText('load failed');
	await expect(page.getByTestId('canvas-readonly')).toBeVisible();
	await expect(page.getByRole('button', { name: 'Rectangle', exact: true })).toHaveCount(0);
});

test('a unit key its dataset cannot name is refused on the canvas in the door’s words', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/pages/tasks/pages-bad?project=pages');

	await expect(page.getByTestId('canvas-refusal')).toContainText(
		"unit key 'letter-001/0/0' has 3 part(s), and this dataset names a unit by 2: doc_id/chunk_id",
	);
});

for (const { who, task, key, reason } of [
	{
		who: 'Viewer',
		task: 'pages-t1',
		key: NAME,
		reason: 'You view this project: the canvas is read-only.',
	},
	{
		who: 'Annotator',
		task: 'pages-held',
		key: 'A letter about the harvest · page 1',
		// By the name the holder last wrote under: the annotator keeps it from their own token.
		reason: 'This item is held by Sam Else: the canvas is read-only.',
	},
]) {
	test(`a reader who may not edit the item (${who} on ${task}) is offered View, and the canvas opens read-only`, async ({
		page,
		context,
	}) => {
		await as(context, who);
		await page.goto('/projects/pages');
		await page
			.getByRole('link', { name: `View ${key}`, exact: true })
			.and(page.locator(`[href="/projects/pages/tasks/${task}?project=pages"]`))
			.click();

		await expect(page.getByTestId('canvas-read-only')).toHaveText(reason);
		await expect(page.getByRole('button', { name: 'Rectangle', exact: true })).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Save', exact: true })).toHaveCount(0);
		await expect(
			page.getByTestId('classification-bar').getByRole('button', { name: 'letter' }),
		).toBeDisabled();
	});
}

test('a task that names several units opens its first, and says so', async ({ page, context }) => {
	await as(context, 'Annotator');
	await page.goto('/projects/pages/tasks/pages-multi?project=pages');

	await expect(page.getByTestId('canvas-one-unit')).toHaveText(
		`This item names 2 units. A task's draft holds one unit's shapes, so the canvas opens its first: ${NAME}.`,
	);
	await expect(page.getByRole('application', { name: CANVAS })).toBeVisible();
});

test('a URL naming another project than the task’s opens the task in its own', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/letters/tasks/pages-t1?project=letters');

	await expect(page).toHaveURL(/\/projects\/pages\/tasks\/pages-t1\?project=pages$/);
	await expect(page.getByTestId('annotate-status')).toContainText('2 annotations');
});

test('the picture is refitted and centred when the canvas’s box changes before the person moves the view', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	// The canvas's box changes after the picture was first fitted, as the filmstrip and the inspector settle.
	await page.setViewportSize({ width: 1100, height: 760 });
	const canvas = page.locator('canvas').first();
	// The picture's columns in the canvas's middle row: as many others on the left as on the right.
	await expect
		.poll(async () => {
			const png = await canvas.screenshot();
			return page.evaluate(
				async (bytes) => {
					const bitmap = await createImageBitmap(
						new Blob([new Uint8Array(bytes)], { type: 'image/png' }),
					);
					const scratch = new OffscreenCanvas(bitmap.width, 1);
					const context2d = scratch.getContext('2d');
					if (!context2d) return Infinity;
					context2d.drawImage(bitmap, 0, -Math.floor(bitmap.height / 2));
					const { data } = context2d.getImageData(0, 0, bitmap.width, 1);
					// The picture is the document's warm hue; the page behind the canvas is neutral grey.
					const painted = (x: number) => (data[x * 4] ?? 0) - (data[x * 4 + 2] ?? 0) > 60;
					let left = 0;
					while (left < bitmap.width && !painted(left)) left += 1;
					let right = bitmap.width - 1;
					while (right > 0 && !painted(right)) right -= 1;
					return Math.abs(left - (bitmap.width - 1 - right));
				},
				[...png],
			);
		})
		.toBeLessThanOrEqual(2);
});

test('at 390 and 320 px the inspector goes under the picture and nothing scrolls sideways', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	for (const width of [390, 320]) {
		await page.setViewportSize({ width, height: 800 });
		await openItem(page, { strip: false });
		await expect(page.locator('[data-layout="stacked"]')).toBeVisible();
		await expect(page.getByRole('navigation', { name: 'Your claimed items' })).toHaveCount(0);
		const overflow = await page.evaluate(() => {
			const toolbar = document.querySelector('[role="toolbar"]');
			return {
				page: document.documentElement.scrollWidth - innerWidth,
				toolbar: toolbar ? toolbar.scrollWidth - toolbar.clientWidth : -1,
			};
		});
		expect(overflow, `at ${width} px`).toEqual({ page: 0, toolbar: 0 });
	}
});

test('a picture the viewer could not sign is signed again by Try again', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await fault(page, who, '/api/signed-media', 'fail');
	await page.goto('/projects/pages/tasks/pages-t1?project=pages');
	await expect(page.getByText('The picture could not be signed.')).toBeVisible();
	await expect(page.getByText('The viewer signs the media')).toBeVisible();

	await fault(page, who, '/api/signed-media', 'clear');
	await page.getByRole('button', { name: /try again/i }).click();
	await expect(page.getByTestId('annotate-status')).toContainText('2 annotations');
});

test('the queue’s next claimed item is one step away, in the stream and the filmstrip', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	const stream = page.getByRole('navigation', { name: 'Your labeling queue' });
	await expect(stream).toContainText('item 1 / 2');
	await expect(
		page.getByRole('navigation', { name: 'Your claimed items' }).getByRole('link'),
	).toHaveCount(2);
	// The step is a page transition, slowed here so the next item has loaded while it still runs: until it ends the
	// browser sends every press to the document root, so the canvas says it is busy and only then ready.
	await page.addStyleTag({
		content:
			'::view-transition-old(*), ::view-transition-new(*) { animation-duration: 3s !important; }',
	});
	await stream.getByRole('link', { name: 'Next item' }).click();
	await expect(page).toHaveURL(/\/projects\/pages\/tasks\/pages-t2\?project=pages$/);
	const region = page.getByRole('application', {
		name: 'Labeling canvas: A map of the harbour · page 1',
	});
	await expect(page.getByTestId('annotate-status')).toContainText(/\d+ annotations?/);
	await expect(region).toHaveAttribute('aria-busy', 'true');
	await expect(region).toHaveAttribute('data-ready', 'true');
	await expect(region).toHaveAttribute('aria-busy', 'false');
	await expect(stream).toContainText('item 2 / 2');
});

test('a drawn box opens the class picker at the shape: its keys class it, Escape discards it, and the panel names what keeps the item from being submitted', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	// The project's crumb is its title and a link back to it.
	await expect(
		page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('link', { name: 'Pages' }),
	).toHaveAttribute('href', /^\/projects\/pages(\?|$)/);
	await expect(page.getByRole('navigation', { name: 'Breadcrumb' })).toContainText(NAME);
	// The tab says the same names, not the ids the URL carries.
	await expect(page).toHaveTitle(`${NAME} · Pages · workbench`);
	const panel = page.getByTestId('task-panel');
	await expect(panel.getByTestId('task-panel-title')).toHaveText('Labeling');

	// Escape on a region just drawn: this task's classes are closed, so the box is not kept unclassed.
	await page.keyboard.press('b');
	const a = await onScreen(page, { x: 70, y: 60 });
	const b = await onScreen(page, { x: 100, y: 90 });
	await page.mouse.move(a.x, a.y);
	await page.mouse.down();
	await page.mouse.move(b.x, b.y, { steps: 4 });
	await page.mouse.up();
	const picker = page.getByTestId('class-picker');
	await expect(picker.getByRole('option')).toHaveText([/figure\s+1/, /caption\s+2/]);
	await page.keyboard.press('Escape');
	await expect(picker).toBeHidden();
	await expect(page.getByTestId('canvas-notice')).toContainText(
		'every region on this task needs a class',
	);
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(2);

	// Drawn again, typed for: the class shows in the list with its colour, and one key changes it.
	await page.mouse.move(a.x, a.y);
	await page.mouse.down();
	await page.mouse.move(b.x, b.y, { steps: 4 });
	await page.mouse.up();
	// Unclassed, it keeps the item from being submitted, and the panel says which region it is.
	await expect(panel.getByTestId('submit-check')).toContainText('1 region without a class: #');
	await expect(panel.getByTestId('task-submit')).toBeDisabled();
	await page.keyboard.type('cap');
	await page.keyboard.press('Enter');
	const list = page.getByTestId('annotation-list');
	await list.getByRole('button', { name: /^caption kept/ }).click();
	await page.keyboard.press('1');
	await expect(page.getByTestId('canvas-notice')).toHaveText('Labeled “figure”');

	await page.getByRole('button', { name: 'Save', exact: true }).click();
	// The shapes' own status is worded as theirs, under the item's state, which is still "Labeling".
	await expect(page.getByTestId('shape-status')).toHaveText(/^Shapes\s*2 kept\s*1 proposed$/);
	// A model's proposal nobody kept or removed keeps the item from being submitted, counted in the annotator's words.
	await expect(panel.getByTestId('submit-check-proposals')).toHaveText(
		'1 proposal on this item is neither kept nor removed: keep or remove it before submitting',
	);
	await expect(panel.getByTestId('task-submit')).toBeDisabled();
	await panel.getByTestId('remove-all-proposals').click();
	await expect(panel.getByTestId('submit-check-proposals')).toHaveCount(0);
	await panel.getByTestId('task-submit').click();
	await expect(panel.getByTestId('task-panel-title')).toHaveText('In review');
});

test('a reviewer opens someone’s submission, reads it as theirs to review, and sends it back with a note', async ({
	page,
	context,
}) => {
	await as(context, 'Reviewer');
	await page.goto('/projects/pages/tasks/pages-review?project=pages');
	const panel = page.getByTestId('task-panel');
	// The submitter by the name they last wrote under, their principal its tooltip nowhere lost.
	await expect(panel.getByTestId('task-panel-title')).toHaveText('Reviewing Sam Else’s work');
	await expect(panel.getByTestId('task-submit')).toHaveCount(0);

	const fired = page.waitForRequest((req) => req.url().includes('/tasks/pages-review/events'));
	await panel.getByTestId('task-request_changes').click();
	const dialog = page.getByTestId('send-back-dialog');
	await dialog.getByRole('textbox', { name: 'What should change' }).fill('Box the legend too.');
	// A note typed is not lost by Escape: the form asks, and keeping it goes back to writing.
	await page.keyboard.press('Escape');
	await dialog
		.getByTestId('send-back-discard')
		.getByRole('button', { name: 'Keep writing' })
		.click();
	await expect(dialog.getByRole('textbox', { name: 'What should change' })).toHaveValue(
		'Box the legend too.',
	);
	await dialog.getByRole('button', { name: 'Send back' }).click();
	expect((await fired).postDataJSON()).toEqual({
		event: 'request_changes',
		message: 'Box the legend too.',
	});
	await expect(panel.getByTestId('task-panel-title')).toHaveText('Changes requested');
	await expect(panel.getByTestId('review-note')).toContainText('Box the legend too.');
});

/** Where `word` (its `nth` occurrence) is drawn in `line`, as a page point at its middle. */
async function wordAt(
	line: Locator,
	word: string,
	nth = 0,
): Promise<{ x: number; y: number; left: number; right: number }> {
	const box = await line.evaluate(
		(el, [w, n]) => {
			// The line's own text: its stretches, without the class tags drawn between them.
			const text = [...el.querySelectorAll('[data-offset]')].map((x) => x.textContent).join('');
			let at = -1;
			for (let i = 0; i <= Number(n); i += 1) at = text.indexOf(String(w), at + 1);
			if (at < 0) return null;
			// Walk the line's stretches (`data-offset`) for the text node holding the word.
			for (const stretch of el.querySelectorAll<HTMLElement>('[data-offset]')) {
				const start = Number(stretch.dataset.offset);
				const node = stretch.firstChild;
				const length = node?.textContent?.length ?? 0;
				const local = at - start;
				if (node && local >= 0 && local < length) {
					const range = document.createRange();
					range.setStart(node, local);
					range.setEnd(node, Math.min(length, local + String(w).length));
					const r = range.getBoundingClientRect();
					return { x: r.x, y: r.y, width: r.width, height: r.height };
				}
			}
			return null;
		},
		[word, nth] as const,
	);
	if (!box) throw new Error(`“${word}” is not drawn in the line`);
	return {
		x: box.x + box.width / 2,
		y: box.y + box.height / 2,
		left: box.x,
		right: box.x + box.width,
	};
}

test('a text item is labelled by selecting its words: the picker classes a span, overlapping spans sit on lanes, an edge moves and comes back, a correction re-anchors them and is saved and read back in code points, and a class key relabels the chosen span', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const violations = await watchCsp(page);
	await page.goto('/projects/notes/tasks/notes-t1?project=notes');
	await expect(page.getByTestId('text-canvas')).toHaveAttribute('data-ready', 'true');
	await expect(page).toHaveTitle(`${NAME} · Entities · workbench`);
	const line = page.getByTestId('text-document-line-0');
	await expect(line).toHaveText('The ships came into the harbour at dawn.');
	const picker = page.getByTestId('class-picker');
	const notice = page.getByTestId('canvas-notice');
	// The side panel speaks of spans on a text.
	await expect(page.getByTestId('submit-check')).toContainText(
		"Nothing is marked yet: mark the item's spans first.",
	);

	// A word double-clicked is a span with its picker open; Escape on a task whose classes are closed discards it.
	let at = await wordAt(line, 'harbour');
	await page.mouse.dblclick(at.x, at.y);
	await expect(picker.getByRole('option')).toHaveText([/person\s+1/, /place\s+2/]);
	await page.keyboard.press('Escape');
	await expect(notice).toContainText('every span on this task needs a class');
	await expect(page.getByTestId('span-tag')).toHaveCount(0);

	// Its digit classes it.
	at = await wordAt(line, 'ships');
	await page.mouse.dblclick(at.x, at.y);
	await page.keyboard.press('1');
	await expect(notice).toHaveText('Labeled “person”');
	// A stretch dragged across it is a second span over the same word, on a lane of its own.
	const from = await wordAt(line, 'ships');
	const to = await wordAt(line, 'came');
	await page.mouse.move(from.left + 1, from.y);
	await page.mouse.down();
	await page.mouse.move(to.right - 1, to.y, { steps: 6 });
	await page.mouse.up();
	await picker.getByRole('option', { name: /place/ }).click();
	await expect(page.getByTestId('span-tag')).toHaveText(['person', 'place']);
	const stretch = (text: string) =>
		line.getByTestId('span-stretch').filter({ hasText: new RegExp(`^${text}$`) });
	await expect(line.getByTestId('span-stretch')).toHaveText(['ships', 'came']);
	await expect(stretch('ships')).toHaveAttribute('title', 'person · place');

	// The selected span's start moves a character with Alt+Shift+→, and an undo puts it back.
	await page.keyboard.press('Alt+Shift+ArrowRight');
	await expect(stretch('hips')).toHaveAttribute('title', 'person · place');
	await expect(stretch('s')).toHaveAttribute('title', 'person');
	await page.keyboard.press('Control+z');
	await expect(stretch('ships')).toHaveAttribute('title', 'person · place');

	const saved = () =>
		page.waitForRequest(
			(req) => req.method() === 'POST' && req.url().includes(`/api/annotations/${UNIT}?`),
		);
	type Save = {
		inserts: {
			id: string;
			shape_type: string;
			text: string;
			source: string;
			label: string;
			parent_id: string;
			char_start: number;
			char_end: number;
		}[];
		edits: { id: string; text?: string }[];
		spans: { id: string; char_start: number; char_end: number }[];
	};
	let save = saved();
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const first = (await save).postDataJSON() as Save;
	const doc = first.inserts.find((i) => i.parent_id === '');
	expect(doc).toMatchObject({
		shape_type: 'text',
		source: 'dataset',
		text: 'The ships came into the harbour at dawn.',
	});
	expect(
		first.inserts
			.filter((i) => i.parent_id !== '')
			.map((i) => [i.label, i.parent_id === doc?.id, i.char_start, i.char_end]),
	).toEqual([
		['person', true, 4, 9],
		['place', true, 4, 14],
	]);

	// A correction moves the words, Enter applies it, and each span follows the words it marked. The new opening is a
	// character outside the BMP: one code point, two UTF-16 units, and a span's offsets are code points.
	const correct = page.getByRole('button', { name: 'Correct the text of line 1' });
	const editor = page.getByRole('textbox', { name: 'Correct line 1' });
	await correct.click();
	await editor.fill('𝔄t dawn the ships came into the harbour.');
	await editor.press('Enter');
	await expect(line).toHaveText(/^𝔄t dawn the ships/);
	await expect(stretch('ships')).toHaveAttribute('title', 'person · place');
	// Ctrl+S with a correction open applies it before it saves.
	await correct.click();
	await editor.fill('𝔄t dawn the ships came into the harbour. Then they left.');
	save = saved();
	await editor.press('Control+s');
	const second = (await save).postDataJSON() as Save;
	expect(second.edits.map((e) => e.text)).toEqual([
		'𝔄t dawn the ships came into the harbour. Then they left.',
	]);
	expect(second.spans.map((e) => [e.char_start, e.char_end])).toEqual([
		[12, 17],
		[12, 22],
	]);

	// Read back: the corrected text and its spans where their words are.
	await page.reload();
	await expect(page.getByTestId('text-canvas')).toHaveAttribute('data-ready', 'true');
	await expect
		.poll(() =>
			line.evaluate((el) =>
				[...el.querySelectorAll('[data-offset]')].map((x) => x.textContent).join(''),
			),
		)
		.toBe('𝔄t dawn the ships came into the harbour. Then they left.');
	await expect(stretch('ships')).toHaveAttribute('title', 'person · place');

	// Text selected and let go of with Escape is not what the next class key labels, nor is it once an annotation is
	// chosen in the list: the key relabels that annotation.
	await line.evaluate((el) => {
		const stretch = [...el.querySelectorAll('[data-offset]')].find((s) =>
			s.textContent?.includes('harbour'),
		);
		const node = stretch?.firstChild;
		const text = node?.textContent ?? '';
		if (!node) return;
		const range = document.createRange();
		range.setStart(node, text.indexOf('harbour'));
		range.setEnd(node, text.indexOf('harbour') + 'harbour'.length);
		window.getSelection()?.removeAllRanges();
		window.getSelection()?.addRange(range);
	});
	await page.keyboard.press('Escape');
	expect(await page.evaluate(() => String(window.getSelection()))).toBe('');
	await page
		.getByTestId('annotation-list')
		.getByRole('button', { name: /^person/ })
		.click();
	await page.keyboard.press('2');
	await expect(notice).toHaveText('Labeled “place”');
	await expect(page.getByTestId('span-tag')).toHaveText(['place', 'place']);
	await expect(page.getByTestId('task-panel').getByTestId('task-submit')).toBeEnabled();
	expect(await violations()).toEqual([]);
});

test('a reviewer compares a consensus item with its other replica’s draft on the picture, takes its shapes into the item, and an undo gives one back', async ({
	page,
	context,
	browser,
}) => {
	await as(context, 'Reviewer');
	await page.goto('/projects/pages/tasks/pagesg-r1?project=pages');
	await ready(page);
	const extra = { x: 96, y: 6, width: 48, height: 38 };
	const before = await settledPixels(page, extra);

	// The other replica's work is read from its own task's draft, never from rows two replicas could share.
	const draft = page.waitForRequest(
		(req) =>
			req.method() === 'GET' && new URL(req.url()).pathname.endsWith('/tasks/pagesg-r2/draft'),
	);
	await page.getByTestId('compare-version').selectOption('pagesg-r2');
	await draft;
	const summary = page.getByTestId('compare-summary');
	await expect(summary).toHaveText('2 differences · 1 alike');
	const differences = page.getByTestId('compare-differences').getByRole('listitem');
	await expect(differences).toHaveText([/differs: caption → figure/, /only theirs: figure · bbox/]);
	// The other replica's box only it has is drawn on the picture, where the item has none.
	expect(Buffer.compare(await settledPixels(page, extra), before)).not.toBe(0);

	await differences
		.filter({ hasText: 'only theirs' })
		.getByRole('button', { name: 'Take' })
		.click();
	await expect(summary).toHaveText('1 difference · 2 alike');
	await differences.filter({ hasText: 'differs' }).getByRole('button', { name: 'Take' }).click();
	await expect(summary).toHaveText('0 differences · 3 alike');
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(3);
	await page.keyboard.press('Control+z');
	await expect(summary).toHaveText('1 difference · 2 alike');

	const save = page.waitForRequest(
		(req) => req.method() === 'POST' && req.url().includes('/api/annotations/letter-002/0?'),
	);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const request = await save;
	expect(new URL(request.url()).searchParams.get('task')).toBe('pagesg-r1');
	const body = request.postDataJSON() as {
		inserts: { label: string; x: number; y: number; status: string }[];
		deletes: string[];
	};
	expect(body.inserts.map((i) => [i.label, i.x, i.y, i.status])).toEqual([
		['figure', 100, 10, 'accepted'],
	]);
	expect(body.deletes).toEqual([]);

	// The holder of a replica of another consensus item, who may not review it, is offered no other replica to compare
	// (nor could read one: the annotator's door refuses it).
	const other = await browser.newContext();
	await as(other, 'Annotator');
	const holder = await other.newPage();
	await holder.goto(`${EDGE}/projects/pairs/tasks/pairs-g1-r1?project=pairs`);
	await ready(holder);
	await expect(holder.getByTestId('task-panel')).toBeVisible();
	await expect(holder.getByTestId('compare-panel')).toHaveCount(0);
	await other.close();
});

/** Every assist call the page sends, as the browser sent it. */
function assistAsks(page: Page): Request[] {
	const asked: Request[] = [];
	page.on('request', (req) => {
		if (
			new URL(req.url()).pathname.startsWith('/api/annotator/api/assist/') &&
			req.method() === 'POST'
		)
			asked.push(req);
	});
	return asked;
}

/** Open the Assist panel, reading the endpoint's models. */
async function openAssist(page: Page): Promise<Locator> {
	await page.getByTestId('assist-open').click();
	return page.getByTestId('assist-panel');
}

const modelRow = (panel: Locator, name: string): Locator =>
	panel.locator(`[data-testid="assist-model"][data-model="${name}"]`);

test('a model asked from the canvas proposes shapes, each naming its model, version and score, and Keep all and Remove all are one action and one undo each', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	const asked = assistAsks(page);
	const panel = await openAssist(page);
	// The endpoint's models by title, and what each is: its name and version. Which prompts each takes is the endpoint's
	// word: the detector is asked with words, the segmenter armed for a box or clicks.
	await expect(panel.getByTestId('assist-model')).toHaveCount(4);
	await expect(modelRow(panel, 'stand-in-detector')).toContainText('Stand-in detector');
	await expect(modelRow(panel, 'stand-in-detector').getByTestId('assist-model-version')).toHaveText(
		'stand-in-detector · stand-in.1 · returns bbox',
	);
	await expect(modelRow(panel, 'stand-in-segmenter').getByRole('button')).toHaveText([
		'Box or click',
	]);
	await panel.getByTestId('assist-prompt').fill('stamp');
	await modelRow(panel, 'stand-in-detector').getByRole('button', { name: 'Find' }).click();

	await expect(page.getByTestId('canvas-notice')).toContainText(
		'1 proposal from stand-in-detector stand-in.1',
	);
	expect(asked[0]?.postDataJSON()).toEqual({
		model: 'stand-in-detector',
		prompt: 'stamp',
		region: null,
		points: [],
		task_id: 'pages-t1',
	});
	expect(new URL(asked[0]?.url() ?? '').pathname).toBe(`/api/annotator/api/assist/${UNIT}`);
	// Each proposal says what made it: the seeded one and the detector's, beside the person's figure, which says nothing.
	const list = page.getByTestId('annotation-list');
	await expect(list.getByTestId('annotation-made-by')).toHaveText([
		'seed-detector · seed.1 · 0.82',
		'stand-in-detector · stand-in.1 · 0.70',
	]);
	await list.getByRole('button', { name: /stand-in-detector/ }).click();
	const detail = page.getByTestId('annotation-detail');
	await expect(detail.getByTestId('annotation-model-version')).toHaveText('stand-in.1');
	await expect(detail.getByTestId('annotation-score')).toHaveText('0.70');
	// A person's figure is never offered as a proposal.
	await page.keyboard.press('Escape');
	await list.getByRole('button', { name: /^figure kept/ }).click();
	await expect(detail.getByTestId('reset-to-proposal')).toHaveCount(0);
	await page.keyboard.press('Escape');

	// The segmenter is armed: each click is a point of its session, the − sign marks the next "not this", and each
	// answer replaces the last, so one proposal stands.
	await openAssist(page);
	await modelRow(panel, 'stand-in-segmenter').getByTestId('assist-arm').click();
	await expect(page.getByTestId('assist-armed')).toContainText('Stand-in segmenter');
	// The panel is put away before the picture takes the clicks.
	await expect(panel).toBeHidden();
	// Clicks off the item's shapes, which a press would otherwise select.
	await clickImage(page, { x: 75, y: 70 });
	await expect.poll(() => asked.length).toBe(2);
	await page.getByTestId('assist-sign').click();
	await clickImage(page, { x: 140, y: 60 });
	await expect.poll(() => asked.length).toBe(3);
	const third = asked[2];
	if (!third) throw new Error('the third assist request was not sent');
	const points = (third.postDataJSON() as { points: { positive: boolean }[] }).points;
	expect(points.map((p) => p.positive)).toEqual([true, false]);
	await expect(page.getByTestId('assist-armed')).toContainText('2 points');
	await expect(list.getByTestId('annotation-made-by')).toHaveText([
		'seed-detector · seed.1 · 0.82',
		'stand-in-detector · stand-in.1 · 0.70',
		'stand-in-segmenter · stand-in.1 · 0.85',
	]);
	await page.getByTestId('assist-done').click();
	await page.getByTestId('assist-disarm').click();

	// Keep all is one action: Alt+A keeps the three, each still naming its model, and one undo gives all three back.
	const shapes = page.getByTestId('shape-status');
	await expect(shapes).toHaveText(/1 kept\s*3 proposed/);
	const panelHead = page.getByTestId('task-panel');
	await expect(panelHead.getByTestId('keep-all-proposals')).toHaveText(/Keep all \(3\)/);
	await page.keyboard.press('Alt+a');
	await expect(shapes).toHaveText(/^Shapes\s*4 kept$/);
	await expect(list.getByTestId('annotation-made-by')).toHaveCount(3);
	await expect(panelHead.getByTestId('keep-all-proposals')).toHaveCount(0);
	await page.keyboard.press('Control+z');
	await expect(shapes).toHaveText(/1 kept\s*3 proposed/);
	// Remove all is one action too, and one undo.
	const count = page.getByTestId('shape-count');
	await panelHead.getByTestId('remove-all-proposals').click();
	await expect(count).toHaveText('1');
	await page.keyboard.press('Control+z');
	await expect(count).toHaveText('4');
	await expect(shapes).toHaveText(/1 kept\s*3 proposed/);

	// The proposals the canvas made are saved as proposals that say what made them.
	const save = saved(page);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const body = (await save).postDataJSON() as {
		inserts: { status: string; source: string; model_version: string; confidence: number }[];
	};
	expect(body.inserts.map((i) => [i.status, i.source, i.model_version, i.confidence])).toEqual([
		['prediction', 'model:stand-in-detector', 'stand-in.1', 0.7],
		['prediction', 'model:stand-in-segmenter', 'stand-in.1', expect.closeTo(0.85, 5)],
	]);
	await expect(page.getByTestId('canvas-notice')).toHaveText('Annotations saved');

	// R turns a proposal down: it is no label, and both the list and its detail say it is not published.
	await list.getByRole('button', { name: /stand-in-segmenter/ }).click();
	await page.keyboard.press('r');
	await page.getByRole('button', { name: 'Back to list' }).click();
	const rejected = list.getByRole('button', { name: /stand-in-segmenter/ });
	await expect(rejected.getByTestId('not-published')).toHaveText('rejected · not published');
	await rejected.click();
	await expect(detail.getByTestId('not-published')).toHaveText('not published');
});

test('an endpoint that does not answer in time, one that fails, and a deployment with none are each said, never as an empty answer', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await openItem(page);
	const list = page.getByTestId('annotation-list');
	await expect(list.getByRole('button')).toHaveCount(2);
	let panel = await openAssist(page);
	await panel.getByTestId('assist-prompt').fill('stamp');
	await modelRow(panel, 'stand-in-slow').getByRole('button', { name: 'Find' }).click();
	const error = page.getByTestId('assist-error');
	await expect(error).toHaveText('Assist failed: the model endpoint did not answer within 30 s');
	await expect(page.getByTestId('canvas-notice')).toHaveText(
		'Assist failed: the model endpoint did not answer within 30 s',
	);
	await expect(list.getByRole('button')).toHaveCount(2);

	panel = await openAssist(page);
	await modelRow(panel, 'stand-in-failing').getByRole('button', { name: 'Find' }).click();
	await expect(error).toHaveText(
		'Assist failed: the model endpoint failed (500): stand-in-failing fails every ask',
	);
	await expect(page.getByTestId('canvas-notice')).not.toContainText('proposal');
	await expect(list.getByRole('button')).toHaveCount(2);

	// A deployment that names no model endpoint offers no model, and says why.
	await fault(page, who, '/api/assist/models', 'no-endpoint');
	await openItem(page);
	panel = await openAssist(page);
	await expect(panel.getByTestId('assist-unavailable')).toHaveText(
		'Assist is unavailable: this deployment names no model endpoint.',
	);
	await expect(panel.getByTestId('assist-model')).toHaveCount(0);
});

test('a reviewer judges three items in one pass by key: accepts one, fixes and accepts one only once its proposal is settled, sends one back', async ({
	page,
	context,
}) => {
	await as(context, 'Reviewer');
	await page.goto('/projects/judging?tab=review');
	await page.getByTestId('review-start').click();
	const panel = page.getByTestId('task-panel');
	const walk = page.getByRole('navigation', { name: 'Reviewing' });
	await ready(page);
	await expect(panel.getByTestId('task-panel-title')).toHaveText('Reviewing Alma Alpha’s work');
	await expect(walk).toContainText('item 1 / 3');

	const accepted = page.waitForRequest((req) => req.url().includes('/tasks/judging-a/events'));
	await page.keyboard.press('Shift+A');
	expect((await accepted).postDataJSON()).toEqual({ event: 'accept' });
	// Where the reviewer is in the pass they started, out of what waited then: not the shrinking remainder.
	await expect(page).toHaveURL(/\/tasks\/judging-b\?of=3&done=1&pass=review/);
	await ready(page);
	await expect(panel.getByTestId('task-panel-title')).toHaveText('Reviewing Bert Beta’s work');
	await expect(walk).toContainText('item 2 / 3');

	// This item holds a proposal nobody kept or removed: it is counted, and neither verdict that accepts it is offered.
	const judged = calls(page, '/tasks/judging-b/events');
	const held =
		'1 proposal on this item is neither kept nor removed: keep or remove it before accepting';
	await expect(panel.getByTestId('review-check-proposals')).toHaveText(held);
	await expect(panel.getByTestId('task-accept')).toBeDisabled();
	await expect(panel.getByTestId('task-fix_and_accept')).toBeDisabled();
	await page.keyboard.press('Shift+A');
	await expect(panel.getByTestId('task-failure')).toHaveText(held);
	expect(judged).toEqual([]);
	// Removed, it is the reviewer's fix: saved with the verdict that carries it.
	const saves = annotationCalls(page);
	await page.keyboard.press('Alt+r');
	await expect(panel.getByTestId('review-check-proposals')).toHaveCount(0);
	const fixed = page.waitForRequest((req) => req.url().includes('/tasks/judging-b/events'));
	await page.keyboard.press('Shift+F');
	expect((await fixed).postDataJSON()).toEqual({ event: 'fix_and_accept' });
	expect(
		saves
			.filter((req) => req.method() === 'POST' && req.url().includes('task=judging-b'))
			.map((req) => (req.postDataJSON() as { deletes: string[] }).deletes),
	).toEqual([['cap-1']]);
	await expect(page).toHaveURL(/\/tasks\/judging-c\?of=3&done=2&pass=review/);
	await ready(page);
	await expect(panel.getByTestId('task-panel-title')).toHaveText('Reviewing Sam Else’s work');
	await expect(walk).toContainText('item 3 / 3');

	await page.keyboard.press('Shift+R');
	const dialog = page.getByTestId('send-back-dialog');
	await dialog.getByRole('textbox', { name: 'What should change' }).fill('Box the seal too.');
	const sentBack = page.waitForRequest((req) => req.url().includes('/tasks/judging-c/events'));
	await dialog.getByRole('button', { name: 'Send back' }).click();
	expect((await sentBack).postDataJSON()).toEqual({
		event: 'request_changes',
		message: 'Box the seal too.',
	});

	// Past the last item the pass ends where it started, with nothing left to review.
	await expect(page).toHaveURL(/\/projects\/judging\?tab=review/);
	await expect(page.getByTestId('review-waiting')).toHaveText(
		'Nothing is waiting for your review.',
	);
});

test('a person browses a project’s labeled work read-only, one item after another, its filter in the address', async ({
	page,
	context,
}) => {
	await as(context, 'Reviewer');
	// The labeled work: an item claimed and never drawn on is not among it.
	await page.goto('/projects/judging?tab=browse');
	await expect(page.getByTestId('browse-list').getByRole('link')).toHaveCount(4);
	await page.goto('/projects/judging?tab=browse&label=figure');
	await expect(page.getByTestId('browse-list').getByRole('link')).toHaveCount(4);
	await page.getByTestId('browse-start').click();

	await ready(page);
	await expect(page).toHaveURL(/\/tasks\/judging-a\?label=figure&pass=browse/);
	const walk = page.getByRole('navigation', { name: 'Browsing' });
	await expect(walk).toContainText('item 1 / 4');
	await expect(page.getByTestId('canvas-read-only')).toHaveText(
		'Browsing: the canvas is read-only.',
	);
	await expect(page.getByRole('button', { name: 'Rectangle', exact: true })).toHaveCount(0);
	await expect(page.getByTestId('task-panel').getByRole('button')).toHaveCount(0);
	// The panel says where the item stands, in browse words, never a labeler's or a reviewer's.
	await expect(page.getByTestId('task-panel-title')).toHaveText('Browsing');
	await expect(page.getByTestId('task-panel')).toContainText(
		'Submitted by Alma Alpha, waiting for a reviewer. You see the item as it was last saved',
	);

	await page.keyboard.press('PageDown');
	await expect(page).toHaveURL(/\/tasks\/judging-b\?label=figure&pass=browse/);
	await expect(walk).toContainText('item 2 / 4');
	await page.reload();
	await expect(page.getByRole('navigation', { name: 'Browsing' })).toContainText('item 2 / 4');
});
