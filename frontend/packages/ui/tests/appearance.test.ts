import { describe, expect, test } from 'vitest';
import {
	ACCENT_TINTS,
	accentVars,
	contrastRgb,
	deriveAccent,
	hexToOklch,
	readAppearance,
	TEXT_CONTRAST,
	writeAppearance,
	type AccentInMode,
	type Rgb,
	type Surfaces,
} from '../src/lib/appearance/index.js';
import { oklchToRgb, over } from '../src/lib/appearance/color.js';

// Surfaces as a page might paint them: deliberately not any theme's, since the real ones are measured from the tokens
// in the browser (the app's e2e `appearance` spec). These span a light page's steps and a dark page's.
const SURFACES: Surfaces = {
	light: [
		[250, 250, 252],
		[236, 233, 244],
		[226, 222, 238],
		[243, 239, 229],
	],
	dark: [
		[20, 20, 24],
		[52, 49, 66],
		[62, 58, 78],
	],
};

/** The least contrast of a mode's accent as text, over every given surface and its own tints over each. */
function leastOnPage(mode: AccentInMode, surfaces: readonly Rgb[]): number {
	const ink = oklchToRgb(mode.color);
	return Math.min(
		...surfaces.flatMap((s) => [
			contrastRgb(ink, s),
			...ACCENT_TINTS.map((a) => contrastRgb(ink, over(ink, a, s))),
		]),
	);
}

describe('a custom accent', () => {
	test.each([
		['a pale yellow, too light for a light page', '#ffe94d'],
		['a pure magenta, too light for its own tint on a light page', '#ff00ff'],
		['a pure blue, too dark for a dark page', '#0000ff'],
		['a mid grey, which reads in neither mode as chosen', '#888888'],
		['black', '#000000'],
		['white', '#ffffff'],
	])('%s is corrected until it reads on every surface and tint, keeping its hue', (_, hex) => {
		const seeds = deriveAccent(hex, SURFACES)!;
		if (hexToOklch(hex)!.c >= 0.02) expect(seeds.hue).toBeCloseTo(hexToOklch(hex)!.h, 6);
		for (const [mode, surfaces] of [
			[seeds.light, SURFACES.light],
			[seeds.dark, SURFACES.dark],
		] as const) {
			expect(leastOnPage(mode, surfaces)).toBeGreaterThanOrEqual(TEXT_CONTRAST);
			expect(mode.contrastOfText).toBeGreaterThanOrEqual(TEXT_CONTRAST);
		}
		expect(seeds.light.lightnessClamped || seeds.dark.lightnessClamped).toBe(true);
	});

	test('a colour that already reads is kept as chosen, its text switched to the side that reads', () => {
		const seeds = deriveAccent('#1f3fbf', SURFACES)!;
		expect(seeds.light.lightnessClamped).toBe(false);
		expect(seeds.light.color.l).toBeCloseTo(hexToOklch('#1f3fbf')!.l, 6);
		expect(seeds.light.on).toBe('light');
		expect(seeds.dark.lightnessClamped).toBe(true);
		expect(seeds.dark.on).toBe('dark');
	});

	test('it is written as the seeds a theme declares, and a non-colour is refused', () => {
		expect(Object.keys(accentVars(deriveAccent('#c0392b', SURFACES)!)).sort()).toEqual([
			'--accent-c-dark',
			'--accent-c-light',
			'--accent-h',
			'--accent-l-dark',
			'--accent-l-light',
			'--on-accent-dark',
			'--on-accent-light',
		]);
		expect(deriveAccent('red', SURFACES)).toBeNull();
	});
});

describe('a kept appearance', () => {
	test('is read field by field: anything it cannot use falls back, and nothing else is kept', () => {
		const hostile = JSON.stringify({
			theme: 'midnight',
			mode: 'dark',
			accent: '#FF00FF',
			vars: { '--background': 'red' },
		});
		const choice = readAppearance(hostile);
		expect(choice).toEqual({ theme: 'ink', mode: 'dark', accent: null });
		expect(JSON.parse(writeAppearance(choice))).toEqual({
			v: 2,
			theme: 'ink',
			mode: 'dark',
			accent: null,
		});
		expect(readAppearance('not json')).toEqual({ theme: 'ink', mode: 'dark', accent: null });
		expect(readAppearance('{"theme":"moss","mode":"system","accent":"#3a5bd9"}')).toEqual({
			theme: 'moss',
			mode: 'system',
			accent: '#3a5bd9',
		});
	});
});
