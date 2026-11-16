/**
 * The atlas's points are graphics a reader must make out on the dark theme: WCAG 2.2 1.4.11 asks 3:1 of each against
 * what is behind it, the dark `--background` (oklch(0.13 0.006 270), sRGB 6 7 10).
 */
import { describe, expect, it } from 'vitest';
import { ATLAS_PALETTE, NOISE_ALPHA, hexToRgb, hueRgb, type Rgb } from './atlas-colors.ts';

const DARK_BACKGROUND: Rgb = { r: 6, g: 7, b: 10 };

function luminance({ r, g, b }: Rgb): number {
	const [lr = 0, lg = 0, lb = 0] = [r, g, b].map((c) => {
		const v = c / 255;
		return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
	});
	return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb;
}

const contrast = (a: Rgb, b: Rgb): number => {
	const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
	return ((hi ?? 0) + 0.05) / ((lo ?? 0) + 0.05);
};

describe('the dark atlas palette', () => {
	it('draws every hue and both greys, the noise as it lands, at 3:1 or more on the dark background', () => {
		const hues = Array.from({ length: 360 }, (_, degree) => hueRgb(degree, 360, true));
		// Noise is drawn at `NOISE_ALPHA` over the background, so its grey is judged as it lands.
		const noise = hexToRgb(ATLAS_PALETTE.dark.noise);
		const a = NOISE_ALPHA / 255;
		const landed: Rgb = {
			r: a * noise.r + (1 - a) * DARK_BACKGROUND.r,
			g: a * noise.g + (1 - a) * DARK_BACKGROUND.g,
			b: a * noise.b + (1 - a) * DARK_BACKGROUND.b,
		};
		const greys = [hexToRgb(ATLAS_PALETTE.dark.other), landed];

		const lowest = Math.min(...[...hues, ...greys].map((rgb) => contrast(rgb, DARK_BACKGROUND)));

		expect(lowest).toBeGreaterThanOrEqual(3);
	});
});
