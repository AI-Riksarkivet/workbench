import { expect, test, type BrowserContext, type Page, type Request } from '@playwright/test';
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
const CANVAS = `Labeling canvas: ${UNIT}`;

async function as(context: BrowserContext, name: string): Promise<MockIdentity> {
	const who = person(name);
	await signIn(context, who);
	return who;
}

/** Open the item's labeling page and wait for its annotations to be on the canvas, and, beside it, for the filmstrip
 *  the queue's read fills: the canvas refits the picture when the strip takes its width, and a click aimed before that
 *  lands beside the shape. */
async function openItem(page: Page, { strip = true } = {}): Promise<void> {
	await page.goto('/projects/pages/tasks/pages-t1?project=pages');
	await expect(page.getByTestId('annotate-status')).toContainText('2 annotations');
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
	await page.keyboard.press('4');
	const a = await onScreen(page, from);
	const b = await onScreen(page, to);
	await page.mouse.move(a.x, a.y);
	await page.mouse.down();
	await page.mouse.move(b.x, b.y, { steps: 4 });
	await page.mouse.up();
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

	await page.getByRole('link', { name: `Annotate ${UNIT}` }).click();

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
			['dataset', 'acme$letters'],
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

test('without WebGPU the canvas says so in the atlas’s words, and offers the way back', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.addInitScript(() =>
		Object.defineProperty(Navigator.prototype, 'gpu', { get: () => undefined }),
	);
	await page.goto('/projects/pages/tasks/pages-t1?project=pages');

	const refusal = page.getByTestId('canvas-gpu-refusal');
	await expect(refusal).toContainText('WebGPU is required (not available in this browser)');
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
		['Select', '1'],
		['Pan', '2'],
		['Lasso (select)', '3'],
		['Rectangle', '4'],
		['Polygon', '5'],
		['Point', '6'],
		['Line', '7'],
		['Pencil (freehand)', '8'],
		['Brush', '9'],
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
	await toolbar.getByRole('button', { name: 'Delete selected' }).focus();
	for (let i = 0; i < 20 && !(await region.evaluate((el) => el === document.activeElement)); i++)
		await page.keyboard.press('Tab');
	await expect(region).toBeFocused();
	await expect(region).not.toHaveCSS('box-shadow', 'none');

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

	// A box: Space anchors it at the cursor, the arrows stretch it, Space commits it.
	await press('4', 'Space', 'Shift+ArrowRight', 'Shift+ArrowRight', 'Shift+ArrowDown', 'Space');
	// A point where the cursor is.
	await press('6', 'Shift+ArrowLeft', 'Space');
	// A polygon: a vertex per Space, Enter closes it.
	await press(
		'5',
		'Shift+ArrowUp',
		'Space',
		'Shift+ArrowRight',
		'Space',
		'Shift+ArrowDown',
		'Space',
		'Enter',
	);
	// A line, held from one Space to the next.
	await press('7', 'Shift+ArrowLeft', 'Space', 'Shift+ArrowLeft', 'Shift+ArrowLeft', 'Space');
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
	await page.keyboard.press('6');
	await clickImage(page, POINT);
	await page.keyboard.press('7');
	const from = await onScreen(page, { x: 110, y: 60 });
	const to = await onScreen(page, { x: 145, y: 25 });
	await page.mouse.move(from.x, from.y);
	await page.mouse.down();
	await page.mouse.move(to.x, to.y, { steps: 5 });
	await page.mouse.up();
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

test('a box drawn with the mouse is saved and read back after a reload', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	await drawBox(page, { x: 100, y: 20 }, { x: 140, y: 60 });
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(3);

	await page.getByRole('button', { name: 'Save', exact: true }).click();
	await expect(page.getByTestId('canvas-notice')).toHaveText('Annotations saved');
	await page.reload();
	await expect(page.getByTestId('annotate-status')).toContainText('3 annotations');
});

test('an edit made while a save is in flight survives it, and is the next save’s', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await openItem(page);
	let release: () => void = () => {};
	const held = new Promise<void>((resolve) => (release = resolve));
	await page.route(`**/api/annotations/${UNIT}?*`, async (route) => {
		if (route.request().method() === 'POST') await held;
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
	await expect(page.getByTestId('canvas-notice')).toHaveText('Annotations saved');

	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(4);
	await page.unroute(`**/api/annotations/${UNIT}?*`);
	const second = saved(page);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const body = (await second).postDataJSON() as { inserts: { x: number }[] };
	expect(body.inserts.map((i) => Math.round(i.x))).toEqual([100]);
});

test('leaving an item saves it first, and a save that fails keeps the person on it', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await openItem(page);
	const next = page
		.getByRole('navigation', { name: 'Your labeling queue' })
		.getByRole('link', { name: 'Next item in your queue' });
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
	await notice.getByRole('button', { name: 'Sign in again' }).click();

	await expect(page).toHaveURL(/\/tasks\/pages-t1\?project=pages$/);
	await expect(notice).toHaveText('Your unsaved edits are back. Save to keep them.');
	await expect(page.getByTestId('annotation-list').getByRole('button')).toHaveCount(3);
	const save = saved(page);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const body = (await save).postDataJSON() as { inserts: unknown[] };
	expect(body.inserts).toHaveLength(1);
});

test('a session the issuer cannot renew is said once, without sending the person round in a loop', async ({
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

	await expect(page.getByTestId('task-signin')).toHaveText(
		'We could not sign you in: reload the page to try again.',
	);
	// The app's own sign-in and the one round trip after the 401.
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

test('a relation is drawn by two picks, undone and redone, and travels with the task’s draft', async ({
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

	// A save writes the shapes and the link into the task's draft.
	await page.getByRole('textbox', { name: 'Group' }).fill('g1');
	const drafted = page.waitForRequest(
		(req) => req.method() === 'PUT' && req.url().endsWith('/api/annotator/tasks/pages-t1/draft'),
	);
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const draft = (await drafted).postDataJSON() as { links: { name: string }[] };
	expect(draft.links.map((l) => l.name)).toEqual(['caption_of']);

	// Reopened, the task shows its link, read back from the draft.
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
	await page.keyboard.press('9');
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
		key: UNIT,
		reason: 'You view this project: the canvas is read-only.',
	},
	{
		who: 'Annotator',
		task: 'pages-held',
		key: 'letter-002/0',
		reason: 'This item is held by user:someone-else: the canvas is read-only.',
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
		`This item names 2 units. A task's draft holds one unit's shapes, so the canvas opens its first: ${UNIT}.`,
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
	await expect(page.getByText('The viewer signs the pictures')).toBeVisible();

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
	await stream.getByRole('link', { name: 'Next item in your queue' }).click();
	await expect(page).toHaveURL(/\/projects\/pages\/tasks\/pages-t2\?project=pages$/);
	await expect(page.getByRole('application', { name: 'Labeling canvas: map-003/0' })).toBeVisible();
	await expect(stream).toContainText('item 2 / 2');
});
