import { expect, test } from '@playwright/test';

// A browser that cannot run the app (ADR 0018), in the `no-webgpu` project (Chromium with no GPU adapter, whose app
// starts and stops in its `init` hook) and the `firefox` project (no WebGPU, stopped by `/browser-check.js` before any
// of the app runs): each is shown one plain page instead of the app.

test('a browser that cannot run the app is told so and what is supported, and the app never starts', async ({
	page,
}, testInfo) => {
	await page.addInitScript(() => {
		const seen = { shell: false };
		(window as unknown as { __seen: typeof seen }).__seen = seen;
		new MutationObserver(() => {
			if (document.querySelector('nav, aside, header, [data-slot="sidebar"]')) seen.shell = true;
		}).observe(document, { subtree: true, childList: true });
	});
	const asked: string[] = [];
	page.on('request', (request) => asked.push(new URL(request.url()).pathname));

	// The browser check stops the document's loading, so its `load` event may never come.
	await page.goto('/datasets', { waitUntil: 'commit' });

	await expect(
		page.getByRole('heading', { level: 1, name: 'This browser cannot run the workbench' }),
	).toBeVisible();
	await expect(page).toHaveTitle('This browser cannot run the workbench');
	await expect(
		page.getByText(
			'The workbench runs in current Chrome or Edge, on a computer with a graphics card. It draws with WebGPU',
		),
	).toBeVisible();

	// None of the app ran: no part of its shell was ever drawn, and it asked for no runtime config, no sign-in and no
	// service.
	expect(
		await page.evaluate(() => (window as unknown as { __seen: { shell: boolean } }).__seen.shell),
	).toBe(false);
	expect(
		asked.filter(
			(path) => path === '/config.json' || path.startsWith('/api/') || path.startsWith('/oidc'),
		),
	).toEqual([]);
	// A browser without WebGPU is stopped before SvelteKit's start script runs (its preload scanner may still fetch the
	// modules); Chromium without an adapter runs it, and the app stops in its `init` hook.
	const started = await page.evaluate(() =>
		Object.keys(window).some((key) => key.startsWith('__sveltekit')),
	);
	expect(started).toBe(testInfo.project.name !== 'firefox');

	// In the mode the person's appearance names (a first visit is dark), with no stylesheet of the app's needed.
	const channels = await page.evaluate(() => {
		// The computed colour may be in any colour space; a canvas reads it back as sRGB.
		const context = document.createElement('canvas').getContext('2d');
		if (!context) return [];
		context.fillStyle = getComputedStyle(document.body).backgroundColor;
		context.fillRect(0, 0, 1, 1);
		return Array.from(context.getImageData(0, 0, 1, 1).data.subarray(0, 3));
	});
	expect(channels).toHaveLength(3);
	expect(Math.max(...channels)).toBeLessThan(80);
});
