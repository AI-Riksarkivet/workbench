import { expect, test, type Page, type Request } from '@playwright/test';
import { person, signIn, watchCsp } from './session';

// The labeling canvas over a recording and a video: the demo's own, whose descriptor, parts, speakers, waveform and film
// the stand-in viewer answers as the real viewer recorded them (`seeded-answers.json`). One time strip carries the
// waveform, the parts, the regions and each track's keyframes; on a video the frame above it is the shared image stage.

const strip = (page: Page) => page.getByTestId('time-strip');

/** The strip's own account of what it draws: each row's stretches and keyframes. */
async function rowSummary(page: Page, row: string): Promise<{ spans: number; markers: number }> {
	const item = strip(page).locator(`[data-row="${row}"]`);
	return {
		spans: Number(await item.getAttribute('data-spans')),
		markers: Number(await item.getAttribute('data-markers')),
	};
}

/** A point on the strip, by fraction of its width and pixels from its top. */
async function onStrip(page: Page, fx: number, y: number): Promise<{ x: number; y: number }> {
	const box = await strip(page).boundingBox();
	if (!box) throw new Error('the time strip has no box');
	return { x: box.x + box.width * fx, y: box.y + y };
}

const saved = (page: Page, unit: string): Promise<Request> =>
	page.waitForRequest(
		(req) => req.method() === 'POST' && req.url().includes(`/api/annotations/${unit}?`),
	);

test('a recording opens on its waveform, a dragged region takes a class from the keys and a transcription, and is saved and read back', async ({
	page,
	context,
}) => {
	await signIn(context, person('Annotator'));
	const violations = await watchCsp(page);
	await page.goto('/projects/sounds/tasks/sounds-t1?project=sounds');
	await expect(page.getByRole('heading', { level: 1 })).toHaveText(
		'Labeling Intervju om hamnen 1952, 0:02–0:04',
	);
	await expect(strip(page)).toHaveAttribute('data-ready', 'true');
	await expect(page.getByTestId('temporal-canvas')).toHaveAttribute('data-ready', 'true');
	expect(await rowSummary(page, 'parts')).toEqual({ spans: 3, markers: 0 });
	expect(await rowSummary(page, 'speaker:Speaker A')).toEqual({ spans: 2, markers: 0 });

	// The waveform lane is 144 px under an 18 px ruler, read once the view stands still on the item's own stretch.
	await expect(strip(page)).toHaveAttribute('data-view', '1.50-4.50');
	const from = await onStrip(page, 0.3, 90);
	const to = await onStrip(page, 0.6, 90);
	await page.mouse.move(from.x, from.y);
	await page.mouse.down();
	await page.mouse.move(to.x, to.y, { steps: 5 });
	await page.mouse.up();
	await expect(page.getByTestId('class-picker')).toBeVisible();
	await page.keyboard.press('1');
	await expect(page.getByTestId('region-label')).toHaveText('question');
	await page.getByTestId('region-transcription').getByRole('textbox').fill('Berätta om hamnen.');
	// The rows' names stand in a gutter beside the axis, so no region drawn from the axis's left edge is under one.
	const name = await page
		.getByTestId('strip-gutter')
		.getByText('Regions', { exact: true })
		.boundingBox();
	const axis = await strip(page).boundingBox();
	expect(name && axis && name.width >= 30 && name.x + name.width <= axis.x).toBe(true);
	expect(await rowSummary(page, 'regions:0')).toEqual({ spans: 1, markers: 0 });

	const save = saved(page, 'c41f0e2a9b7d3e15/0/1');
	await page.keyboard.press('Control+s');
	const body = (await save).postDataJSON() as {
		inserts: { shape_type: string; label: string; text: string; t_start: number; t_end: number }[];
	};
	expect(body.inserts.map((r) => [r.shape_type, r.label, r.text])).toEqual([
		['segment', 'question', 'Berätta om hamnen.'],
	]);
	const [region] = body.inserts;
	expect(region && region.t_end - region.t_start).toBeGreaterThan(0.3);
	await expect(page.getByTestId('task-panel')).toContainText('Every region has a class');

	await page.reload();
	await expect(strip(page)).toHaveAttribute('data-ready', 'true');
	await expect(page.getByTestId('temporal-canvas')).toHaveAttribute('data-ready', 'true');
	await expect(page.getByTestId('region-label')).toHaveText('question');
	expect(await violations()).toEqual([]);
});

test('a region is marked from the keys, its end nudged 10 ms and the nudge undone with Ctrl+Z, and saved as its times', async ({
	page,
	context,
}) => {
	await signIn(context, person('Annotator'));
	await page.goto('/projects/sounds/tasks/sounds-t1?project=sounds');
	await expect(strip(page)).toHaveAttribute('data-ready', 'true');
	await expect(page.getByTestId('temporal-canvas')).toHaveAttribute('data-ready', 'true');
	const canvas = page.getByRole('application', { name: /^Labeling canvas:/ });
	await canvas.focus();
	// A region marked from the keys: [ at the playhead, the playhead moved a second on, ] at its end.
	await page.keyboard.press('[');
	await page.keyboard.press('Shift+ArrowRight');
	await page.keyboard.press(']');
	await expect(page.getByTestId('class-picker')).toBeVisible();
	await page.keyboard.press('2');
	await expect(page.getByTestId('region-label')).toHaveText('answer');
	const first = saved(page, 'c41f0e2a9b7d3e15/0/1');
	await page.keyboard.press('Control+s');
	const inserted = (await first).postDataJSON() as {
		inserts: { id: string; t_start: number; t_end: number }[];
	};
	const [made] = inserted.inserts;
	expect(made && Math.round((made.t_end - made.t_start) * 10) / 10).toBe(1);
	await expect(page.getByTestId('temporal-save')).toContainText('saved');

	// Its end, nudged 10 ms later from the keys, then undone; then later again.
	await page.getByTestId('region-label').waitFor();
	await page.keyboard.press('n');
	await page.keyboard.press('>');
	await page.keyboard.press('Control+z');
	await page.keyboard.press('>');
	const second = saved(page, 'c41f0e2a9b7d3e15/0/1');
	await page.keyboard.press('Control+s');
	const moved = (await second).postDataJSON() as {
		temporal: { id: string; t_start: number; t_end: number }[];
	};
	expect(moved.temporal.map((t) => Math.round((t.t_end - (made?.t_end ?? 0)) * 1000))).toEqual([
		10,
	]);
});

test('a video’s frame is drawn by the image stage, and a box drawn on it becomes a track, stepped and keyframed from the frame, listed once', async ({
	page,
	context,
}) => {
	await signIn(context, person('Annotator'));
	await page.goto('/projects/films/tasks/films-t1?project=films');
	const frame = page.getByTestId('video-frame');
	await expect(frame.getByTestId('pixi-canvas')).toHaveAttribute('data-renderer', 'webgpu');
	await expect(page.getByTestId('annotate-status')).toBeHidden();
	await expect(strip(page)).toHaveAttribute('data-ready', 'true');
	await expect(page.getByTestId('temporal-canvas')).toHaveAttribute('data-ready', 'true');
	expect(await rowSummary(page, 'parts')).toEqual({ spans: 2, markers: 0 });

	const box = await frame.boundingBox();
	if (!box) throw new Error('the frame has no box');
	const at = (fx: number, fy: number) => ({
		x: box.x + box.width * fx,
		y: box.y + box.height * fy,
	});
	await page.getByRole('button', { name: 'Rectangle', exact: true }).click();
	const a = at(0.3, 0.35);
	const b = at(0.45, 0.55);
	await page.mouse.move(a.x, a.y);
	await page.mouse.down();
	await page.mouse.move(b.x, b.y, { steps: 4 });
	await page.mouse.up();
	await page.keyboard.press('2');
	await expect(strip(page).locator('[data-row^="track:"]')).toHaveAttribute('data-markers', '1');
	// Each control has a name of its own: no other button's name contains the Select tool's.
	await expect(page.getByRole('button', { name: 'Select' })).toHaveCount(1);

	// A second on, the selected box moved makes a keyframe there, the frames between interpolated.
	await page.keyboard.press('v');
	// The time keys work wherever the focus is on the item, not only on the strip: here, nowhere in particular.
	await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
	expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
	await page.keyboard.press('Shift+ArrowRight');
	await expect(page.getByTestId('clock')).toContainText('0:01');
	const c = at(0.375, 0.45);
	const d = at(0.55, 0.45);
	await page.mouse.move(c.x, c.y);
	await page.mouse.down();
	await page.mouse.move(d.x, d.y, { steps: 4 });
	await page.mouse.up();
	await expect(strip(page).locator('[data-row^="track:"]')).toHaveAttribute('data-markers', '2');
	// T copies the box to the next frame as a keyframe and steps there.
	await page.keyboard.press('t');
	await expect(strip(page).locator('[data-row^="track:"]')).toHaveAttribute('data-markers', '3');

	// One object, one entry: the list, its count and the layers count the track once, with its keyframes.
	await page.keyboard.press('Escape');
	const list = page.getByTestId('annotation-list');
	await expect(list.getByRole('button')).toHaveCount(1);
	await expect(list.getByTestId('track-entry')).toHaveText(
		/^track · 3 keyframes · 0:00\.0–0:01\.\d$/,
	);
	await expect(page.getByTestId('shape-count')).toHaveText('1');
	await expect(page.getByTestId('layer-panel').getByText('1', { exact: true })).toBeVisible();

	const save = saved(page, 'f1a07c3e9d2b4e68/0/0');
	await page.keyboard.press('Control+s');
	const body = (await save).postDataJSON() as {
		inserts: { shape_type: string; label: string; group: string; t_start: number }[];
	};
	expect(body.inserts.map((r) => [r.shape_type, r.label, Math.round(r.t_start * 10) / 10])).toEqual(
		[
			['bbox', 'cart', 0],
			['bbox', 'cart', 1],
			['bbox', 'cart', 1],
		],
	);
	expect(new Set(body.inserts.map((r) => r.group)).size).toBe(1);
});
