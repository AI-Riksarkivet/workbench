import { expect, type Locator, type Page } from '@playwright/test';

/** How many pixels in the middle of a canvas carry colour: the points and nodes are coloured, the
 *  cleared background and its dot grid are grey, and the overlays sit at the edges. Read from a screenshot,
 *  since a WebGPU canvas's frame is not the page's to read back. */
export async function drawnPixels(
	page: Page,
	canvas: Locator,
	{ whole = false }: { whole?: boolean } = {},
): Promise<number> {
	await expect(canvas).toBeVisible();
	const png = await canvas.screenshot();
	return page.evaluate(
		async ([bytes, all]) => {
			const bitmap = await createImageBitmap(
				new Blob([new Uint8Array(bytes)], { type: 'image/png' }),
			);
			// The middle half, or (`whole`, for a few nodes the layout spreads to the edges) all of it.
			const share = all ? 1 : 0.5;
			const [w, h] = [Math.floor(bitmap.width * share), Math.floor(bitmap.height * share)];
			const scratch = new OffscreenCanvas(w, h);
			const context = scratch.getContext('2d');
			if (!context) return 0;
			context.drawImage(
				bitmap,
				(-bitmap.width * (1 - share)) / 2,
				(-bitmap.height * (1 - share)) / 2,
			);
			const { data } = context.getImageData(0, 0, w, h);
			let drawn = 0;
			for (let i = 0; i < data.length; i += 4) {
				const rgb = [data[i] ?? 0, data[i + 1] ?? 0, data[i + 2] ?? 0];
				if (Math.max(...rgb) - Math.min(...rgb) > 60) drawn += 1;
			}
			return drawn;
		},
		[[...png], whole] as const,
	);
}
