import { readFileSync } from 'node:fs';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { textContrast } from './contrast';
import { serveRecording } from './recording';
import { person, signIn, watchCsp } from './session';

// The dataset page's viewer, on the built app under its real CSP: a document's pages on the shared WebGPU image stage
// (Chromium's software adapter, the `webgpu` project in `playwright.config.ts`), and a recording's transport and
// transcript on the real media element.

const documents = (page: Page) => page.getByRole('list', { name: 'Documents' });
const player = (page: Page) => page.getByRole('region', { name: 'Player' });

async function open(page: Page, title: RegExp): Promise<void> {
	await documents(page).getByRole('button', { name: title }).click();
}

/** The colour at the middle of the image stage, read from a screenshot: a WebGPU canvas's frame is not the page's to
 *  read back. */
async function centreColour(page: Page, stage: Locator): Promise<[number, number, number]> {
	const png = await stage.screenshot();
	return page.evaluate(
		async (bytes) => {
			const bitmap = await createImageBitmap(
				new Blob([new Uint8Array(bytes)], { type: 'image/png' }),
			);
			const scratch = new OffscreenCanvas(1, 1);
			const context = scratch.getContext('2d');
			if (!context) return [0, 0, 0] as [number, number, number];
			context.drawImage(bitmap, -Math.floor(bitmap.width / 2), -Math.floor(bitmap.height / 2));
			const [r = 0, g = 0, b = 0] = context.getImageData(0, 0, 1, 1).data;
			return [r, g, b] as [number, number, number];
		},
		[...png],
	);
}

/** A colour, not the grey of an empty stage: the picture is drawn. */
const coloured = ([r, g, b]: [number, number, number]): boolean =>
	Math.max(r, g, b) - Math.min(r, g, b) > 40;

/** Each word of a line, timed evenly across it, with the space before it as the aligner writes it. */
function timedWords(text: string, start: number, end: number) {
	const words = text.split(' ');
	const step = (end - start) / words.length;
	return words.map((word, i) => ({
		text: i === 0 ? word : ` ${word}`,
		start: start + i * step,
		end: start + (i + 1) * step,
	}));
}

/** The stand-in's letters as a timed recording (or video): each chunk a three-second segment, its
 *  text one aligned line (`lines` replaces a chunk's text), and the media a real WAV. */
async function asRecording(
	page: Page,
	kind: 'audio' | 'video',
	lines: Record<number, string> = {},
): Promise<void> {
	await page.route('**/api/viewer/api/datasets/*/descriptor', async (route) => {
		const answer = await route.fetch();
		const body = await answer.json();
		body.declared.document.kind = kind;
		body.declared.time = { start: 'start', end: 'end' };
		body.declared.capabilities = { ...body.declared.capabilities, alignments: 'chunks.alignments' };
		await route.fulfill({ response: answer, json: body });
	});
	await page.route('**/api/viewer/api/doc-chunks/**', async (route) => {
		const answer = await route.fetch();
		const body = await answer.json();
		body.chunks = body.chunks.map((chunk: { text: string }, i: number) => {
			const text = lines[i] ?? chunk.text;
			const [start, end] = [i * 3, i * 3 + 3];
			return {
				...chunk,
				text,
				start,
				end,
				alignments: [{ start, end, text, words: timedWords(text, start, end) }],
			};
		});
		await route.fulfill({ response: answer, json: body });
	});
	await serveRecording(page);
	// The waveform, as the viewer answered it for one of the demo's recordings (`seeded-answers.json`).
	const recorded = JSON.parse(
		readFileSync(new URL('./seeded-answers.json', import.meta.url), 'utf8'),
	) as Record<string, { json?: unknown }>;
	const peaks =
		recorded['/api/datasets/lake~demo$recordings/documents/c41f0e2a9b7d3e15/peaks']?.json;
	await page.route('**/api/viewer/api/datasets/*/documents/*/peaks', (route) =>
		route.fulfill({ json: peaks }),
	);
}

test('a document’s pages are turned with the buttons and the keys, and each page’s own picture is drawn', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	const violations = await watchCsp(page);
	await page.goto('/datasets/lake~acme%24letters');
	await open(page, /A letter from the harbour/);

	const viewer = player(page);
	await expect(viewer.getByRole('heading', { name: 'A letter from the harbour' })).toBeVisible();
	await expect(viewer.getByTestId('viewer-position')).toHaveText('Page 1 of 2');
	const stage = viewer.getByTestId('pixi-canvas');
	await expect(stage).toHaveAttribute('data-renderer', 'webgpu');
	await expect(
		viewer.getByRole('img', { name: 'A letter from the harbour, page 1 of 2' }),
	).toBeVisible();
	await expect.poll(async () => coloured(await centreColour(page, stage))).toBe(true);
	const first = await centreColour(page, stage);

	await viewer.getByRole('button', { name: 'Next page' }).click();
	await expect(viewer.getByTestId('viewer-position')).toHaveText('Page 2 of 2');
	await expect(viewer.getByRole('button', { name: 'Next page' })).toBeDisabled();
	await expect(
		viewer.getByRole('img', { name: 'A letter from the harbour, page 2 of 2' }),
	).toBeVisible();
	await expect
		.poll(async () => {
			const now = await centreColour(page, stage);
			return coloured(now) && now.some((c, i) => Math.abs(c - (first[i] ?? 0)) > 40);
		})
		.toBe(true);

	// The keys, on the focused page: the arrows and Page Up/Down turn it.
	await viewer.getByRole('group', { name: /page 2 of 2/ }).focus();
	await page.keyboard.press('ArrowLeft');
	await expect(viewer.getByTestId('viewer-position')).toHaveText('Page 1 of 2');
	await page.keyboard.press('PageDown');
	await expect(viewer.getByTestId('viewer-position')).toHaveText('Page 2 of 2');
	expect(await violations()).toEqual([]);
});

test('a page is zoomed with its controls, the wheel and the keyboard, and fitted again', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.goto('/datasets/lake~acme%24letters');
	await open(page, /A map of the harbour/);

	const viewer = player(page);
	const stage = viewer.getByTestId('pixi-canvas');
	await expect(stage).toHaveAttribute('aria-busy', 'false');
	const level = viewer.getByTestId('zoom-level');
	const percent = async () => Number((await level.textContent())?.replace('%', ''));
	const fit = await percent();
	expect(fit).toBeGreaterThan(0);

	await viewer.getByRole('button', { name: 'Zoom in' }).click();
	expect(await percent()).toBeGreaterThan(fit);
	await viewer.getByRole('button', { name: 'Actual size' }).click();
	await expect(level).toHaveText('100%');
	await viewer.getByRole('button', { name: 'Fit to view' }).click();
	await expect(level).toHaveText(`${fit}%`);

	const box = (await stage.boundingBox())!;
	await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
	await page.mouse.wheel(0, -300);
	await expect.poll(percent).toBeGreaterThan(fit);

	await viewer.getByRole('group', { name: 'A map of the harbour', exact: true }).focus();
	await page.keyboard.press('0');
	await expect(level).toHaveText(`${fit}%`);
	await page.keyboard.press('+');
	await expect.poll(percent).toBeGreaterThan(fit);
	await page.keyboard.press('-');
	await page.keyboard.press('-');
	await expect.poll(percent).toBeLessThan(fit);
});

test('media the viewer cannot sign is said plainly in the player, with no image, and a retry shows it', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.route('**/api/viewer/api/signed-media', (route) =>
		route.request().method() === 'POST'
			? route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } })
			: route.continue(),
	);
	await page.goto('/datasets/lake~acme%24letters');
	await open(page, /A map of the harbour/);

	const viewer = player(page);
	await expect(viewer.getByRole('alert')).toHaveText("This item's media could not be loaded.");
	await expect(viewer.getByRole('img')).toHaveCount(0);

	await page.unroute('**/api/viewer/api/signed-media');
	await viewer.getByRole('button', { name: 'Try again' }).click();
	await expect(viewer.getByRole('img', { name: 'A map of the harbour' })).toBeVisible();
	await expect(viewer.getByRole('alert')).toHaveCount(0);
});

for (const { kind, noun } of [
	{ kind: 'audio', noun: 'recording' },
	{ kind: 'video', noun: 'video' },
] as const) {
	test(`a ${noun} plays from the design system's transport: play, the keys, speed, mute, and its waveform and segments on the time strip`, async ({
		page,
		context,
	}) => {
		await signIn(context, person('Ada'));
		await asRecording(page, kind);
		await page.goto('/datasets/lake~acme%24letters');
		await open(page, /A letter from the harbour/);

		const viewer = player(page);
		const element = viewer.locator(kind);
		await expect(element).toHaveAttribute('src', /\/api\/signed-media\//);
		// The transport is the page's own: the element draws no controls of its own.
		expect(await element.getAttribute('controls')).toBeNull();
		const transport = viewer.getByRole('group', { name: 'Playback' });
		// The time strip above it: the waveform the viewer decodes, and the two three-second segments on their lane.
		const strip = viewer.getByTestId('time-strip');
		await expect(strip).toHaveAttribute('data-ready', 'true');
		await expect(strip.locator('[data-row="parts"]')).toHaveAttribute('data-spans', '2');

		// Opened, it plays from the row's start; Pause stops it where it is.
		await transport.getByRole('button', { name: 'Pause' }).click();
		await expect(transport.getByRole('button', { name: 'Play', exact: true })).toBeVisible();
		expect(await element.evaluate((el: HTMLMediaElement) => el.paused)).toBe(true);

		// The scrubber is a slider, named, read as a clock, and its arrows step five seconds.
		const scrubber = transport.getByRole('slider', { name: `Seek in the ${noun}` });
		await scrubber.focus();
		await page.keyboard.press('Home');
		await expect(scrubber).toHaveAttribute('aria-valuetext', '0:00 of 0:30');
		await page.keyboard.press('ArrowRight');
		await expect(scrubber).toHaveAttribute('aria-valuetext', '0:05 of 0:30');
		await expect
			.poll(() => element.evaluate((el: HTMLMediaElement) => Math.round(el.currentTime)))
			.toBe(5);
		await expect(viewer.getByTestId('viewer-position')).toHaveText('Segment 2 of 2 · 0:05');

		// Space on the viewer plays and pauses, from the start, so the recording does not run out while it plays.
		await scrubber.focus();
		await page.keyboard.press('Home');
		await expect(scrubber).toHaveAttribute('aria-valuetext', '0:00 of 0:30');
		await viewer.getByRole('heading').click();
		await page.keyboard.press('Space');
		await expect(transport.getByRole('button', { name: 'Pause' })).toBeVisible();
		await page.keyboard.press('Space');
		await expect(transport.getByRole('button', { name: 'Play', exact: true })).toBeVisible();

		await transport.getByRole('button', { name: /^Playback speed 1×/ }).click();
		expect(await element.evaluate((el: HTMLMediaElement) => el.playbackRate)).toBe(1.25);
		await transport.getByRole('button', { name: 'Mute' }).click();
		expect(await element.evaluate((el: HTMLMediaElement) => el.muted)).toBe(true);
		await expect(transport.getByRole('button', { name: 'Unmute' })).toBeVisible();
	});
}

test('a video whose link stops serving plays on from a renewed link, which the closing stage leaves on the element', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await asRecording(page, 'video');
	// Every time the page empties a media element's source (`src = ""`, which stops and unloads it) is recorded.
	await page.addInitScript(() => {
		const emptied: string[] = [];
		Object.assign(window, { emptied });
		const src = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
		Object.defineProperty(HTMLMediaElement.prototype, 'src', {
			...src,
			set(this: HTMLMediaElement, value: string) {
				if (value === '') emptied.push(this.currentSrc);
				src?.set?.call(this, value);
			},
		});
	});
	// A real film (the demo's, as the viewer served it: `seeded-answers.json`), so the stage draws its frames. The first
	// link of the element serves the film's start and then stops serving, as a link that expires mid-play does; every
	// later link serves it whole.
	const recorded = JSON.parse(
		readFileSync(new URL('./seeded-answers.json', import.meta.url), 'utf8'),
	) as Record<string, { bytes?: string }>;
	const film = Buffer.from(recorded['/__media/f1a07c3e9d2b4e68']?.bytes ?? '', 'base64');
	let first: string | null = null;
	await page.route('**/api/viewer/api/signed-media/*', async (route) => {
		const url = route.request().url();
		const range = /bytes=(\d+)-(\d*)/.exec(route.request().headers()['range'] ?? '');
		// The element asks in byte ranges; a picture's link (the row's thumbnail) does not, and is the stand-in's.
		if (!range) return route.fallback();
		first ??= url;
		const from = Number(range[1]);
		if (url === first && from > 0) {
			// Refused once the first frames are drawn, as a link expires while the film plays.
			await new Promise((done) => setTimeout(done, 1500));
			return route.fulfill({ status: 403, json: { detail: 'the media URL expired' } });
		}
		const to = url === first ? Math.min(film.length, 81920) - 1 : film.length - 1;
		return route.fulfill({
			status: 206,
			body: film.subarray(from, to + 1),
			contentType: 'video/webm',
			headers: { 'accept-ranges': 'bytes', 'content-range': `bytes ${from}-${to}/${film.length}` },
		});
	});
	await page.goto('/datasets/lake~acme%24letters');
	await open(page, /A letter from the harbour/);

	const element = player(page).locator('video');
	// A fresh link is signed and the element plays from it.
	await expect
		.poll(() =>
			element.evaluate((el: HTMLVideoElement, old) => (el.src !== old ? el.src : ''), first),
		)
		.toMatch(/\/api\/signed-media\//);
	await expect
		.poll(() => element.evaluate((el: HTMLVideoElement) => el.readyState), { timeout: 10_000 })
		.toBeGreaterThanOrEqual(2);
	await expect(element).toHaveAttribute('src', /\/api\/signed-media\//);
	await expect(player(page).getByRole('alert')).toHaveCount(0);
	// Nothing emptied the element on the way: the stage drawing its frames leaves the element's source to its holder.
	expect(await page.evaluate(() => (window as unknown as { emptied: string[] }).emptied)).toEqual(
		[],
	);
});

test('the transcript follows the playhead, a line seeks, and no line is clipped at the narrow width', async ({
	page,
	context,
}) => {
	await signIn(context, person('Ada'));
	await page.setViewportSize({ width: 420, height: 900 });
	await asRecording(page, 'audio', {
		1: 'The harbour master counted twelve ships and wrote every name into the Hamnkaptenensräkenskapsbokföringsprotokoll before nightfall.',
	});
	await page.goto('/datasets/lake~acme%24letters');
	await open(page, /A letter from the harbour/);

	const viewer = player(page);
	const transcript = viewer.getByRole('region', { name: 'Transcript' });
	const lines = transcript.getByRole('listitem');
	await expect(lines).toHaveCount(2);
	await viewer
		.getByRole('group', { name: 'Playback' })
		.getByRole('button', { name: 'Pause' })
		.click();

	// A line pressed plays from its start, and is the current line with its current word marked.
	await lines.nth(1).getByRole('button').click();
	await expect(lines.nth(1).getByRole('button')).toHaveAttribute('aria-current', 'true');
	await expect(lines.nth(0).getByRole('button')).not.toHaveAttribute('aria-current', 'true');
	expect(
		await viewer.locator('audio').evaluate((el: HTMLMediaElement) => el.currentTime),
	).toBeGreaterThanOrEqual(3);
	await viewer
		.getByRole('group', { name: 'Playback' })
		.getByRole('button', { name: 'Pause' })
		.click();
	const word = lines.nth(1).locator('[data-current-word]');
	await expect(word).toHaveCount(1);

	// Every line wraps inside the transcript at this width: none is wider than its box or runs past the panel.
	const clipped = await transcript.evaluate((region) => {
		const edge = region.getBoundingClientRect().right;
		return [...region.querySelectorAll('li button')]
			.filter(
				(line) =>
					line.scrollWidth > line.clientWidth + 1 || line.getBoundingClientRect().right > edge + 1,
			)
			.map((line) => line.textContent);
	});
	expect(clipped).toEqual([]);

	// The current word reads at 4.5:1 or better, in light and in dark.
	expect(await textContrast(word)).toBeGreaterThanOrEqual(4.5);
	await page.emulateMedia({ colorScheme: 'dark' });
	await expect(page.locator('html')).toHaveClass(/dark/);
	expect(await textContrast(word)).toBeGreaterThanOrEqual(4.5);
});
