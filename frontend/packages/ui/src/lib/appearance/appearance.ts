/**
 * An accent of the person's own, derived from one colour and corrected until it reads on the page it is drawn on.
 *
 * The accent is drawn as text (a link, a selected label), as a mark and as the focus ring on every surface a theme
 * paints (`ACCENT_SURFACES`), and on its own tints (`ACCENT_TINTS`: a selected chip is the accent at a tenth over its
 * surface). Those surfaces are not known here: the store MEASURES them from the page's tokens, for the active
 * theme, in light and in dark (`appearance.svelte.ts`), so what is checked is what is painted, whichever theme the
 * accent is put on. The result is the same seeds a theme declares (`themes.css`), set on <html>.
 */
import {
	clampChroma,
	contrastRgb,
	cssOklch,
	hexToOklch,
	oklchToRgb,
	over,
	type Oklch,
	type Rgb,
} from './color.js';

/** Every surface token the accent can be drawn on, as text, a mark or a ring: each theme's surface steps. */
export const ACCENT_SURFACES = [
	'--background',
	'--card',
	'--popover',
	'--muted',
	'--accent',
	'--secondary',
	'--sidebar',
	'--sidebar-accent',
	'--input',
] as const;

/** The accent's own tint under accent text: a selected chip or link is the accent at a tenth over its surface
 *  (`bg-primary/10` with `text-primary`). Heavier tints carry the foreground, never the accent, as text. */
export const ACCENT_TINTS = [0.1] as const;

/** AA for text, and for the text drawn on a filled control. */
export const TEXT_CONTRAST = 4.5;

/** What the correction aims for: AA with a little room, since the browser's own OKLCH to sRGB conversion may round a
 *  channel the other way from this one. */
const TARGET = TEXT_CONTRAST + 0.08;

/** How far a hovered fill moves away from the page (`--primary-hover` in `tokens.css`). */
const HOVER_STEP = 0.05;

/** The painted surfaces, in each mode, the accent must read on. */
export type Surfaces = { light: readonly Rgb[]; dark: readonly Rgb[] };

/** The accent in one mode: its lightness and chroma, the text drawn on it, and what had to change. */
export type AccentInMode = {
	color: Oklch;
	/** Light text on it, or dark. */
	on: 'light' | 'dark';
	/** The least contrast of the accent as text, over every surface and tint. */
	contrastOnPage: number;
	/** The least contrast of the text drawn on the accent, at rest and hovered. */
	contrastOfText: number;
	/** The chosen lightness had to move for every pair to read (it is never moved otherwise). */
	lightnessClamped: boolean;
};

export type AccentSeeds = { hue: number; light: AccentInMode; dark: AccentInMode };

/** The text drawn on the accent in each case: near-white or near-black, in the accent's own hue. */
const onColor = (on: 'light' | 'dark', h: number): Oklch =>
	on === 'light' ? { l: 0.99, c: 0.004, h } : { l: 0.16, c: 0.02, h };

/** The least contrast of `color` as text on every surface and on its own tints over each. */
function leastOnPage(color: Oklch, surfaces: readonly Rgb[]): number {
	const ink = oklchToRgb(color);
	let least = Infinity;
	for (const surface of surfaces) {
		least = Math.min(least, contrastRgb(ink, surface));
		for (const alpha of ACCENT_TINTS)
			least = Math.min(least, contrastRgb(ink, over(ink, alpha, surface)));
	}
	return least;
}

/**
 * One mode's accent from the chosen colour, against that mode's painted surfaces. The chosen lightness is kept when
 * every pair reads. Otherwise it is CLAMPED, darker in light mode and lighter in dark, in small steps, until the accent
 * reads as text on the worst of them and on its own tints; the text on it is SWITCHED to whichever of near-white or
 * near-black reads better at rest and hovered, and the clamp continues if even that falls short. Chroma follows the
 * sRGB gamut at each lightness, so the hue is the chosen one.
 */
export function accentFor(
	chosen: Oklch,
	mode: 'light' | 'dark',
	surfaces: readonly Rgb[],
): AccentInMode {
	const dir = mode === 'light' ? 1 : -1;
	let l = Math.min(0.92, Math.max(0.22, chosen.l));
	for (let i = 0; i < 400; i++) {
		const color = clampChroma({ l, c: chosen.c, h: chosen.h });
		const hover = clampChroma({ l: l - HOVER_STEP * dir, c: chosen.c, h: chosen.h });
		const onPage = leastOnPage(color, surfaces);
		const textOf = (on: 'light' | 'dark') => {
			const text = oklchToRgb(onColor(on, chosen.h));
			return Math.min(contrastRgb(oklchToRgb(color), text), contrastRgb(oklchToRgb(hover), text));
		};
		const light = textOf('light');
		const dark = textOf('dark');
		const on = light >= dark ? 'light' : 'dark';
		const ofText = Math.max(light, dark);
		if (onPage >= TARGET && ofText >= TARGET) {
			return {
				color,
				on,
				contrastOnPage: onPage,
				contrastOfText: ofText,
				lightnessClamped: Math.abs(l - chosen.l) > 1e-6,
			};
		}
		const next = l - 0.0025 * dir;
		if (next <= 0 || next >= 1) break;
		l = next;
	}
	// Surfaces no accent can read on (none a theme ships): the mode's own text colour, so nothing is unreadable.
	const color = { l: mode === 'light' ? 0.2 : 0.95, c: 0, h: chosen.h };
	return {
		color,
		on: mode === 'light' ? 'light' : 'dark',
		contrastOnPage: leastOnPage(color, surfaces),
		contrastOfText: contrastRgb(
			oklchToRgb(color),
			oklchToRgb(onColor(mode === 'light' ? 'light' : 'dark', chosen.h)),
		),
		lightnessClamped: true,
	};
}

/** A grey has no hue of its own; it keeps a trace of chroma so the tints still read as tints. */
export function chosenColour(hex: string): Oklch | null {
	const chosen = hexToOklch(hex);
	if (!chosen) return null;
	return chosen.c < 0.02 ? { ...chosen, c: 0.02 } : chosen;
}

/** The accent seeds for one chosen colour (`#rrggbb`) on the given surfaces, or null when it is not one. */
export function deriveAccent(hex: string, surfaces: Surfaces): AccentSeeds | null {
	const base = chosenColour(hex);
	if (!base) return null;
	return {
		hue: base.h,
		light: accentFor(base, 'light', surfaces.light),
		dark: accentFor(base, 'dark', surfaces.dark),
	};
}

/** The inline custom properties a custom accent sets on <html>: the same seeds a theme declares. */
export function accentVars(seeds: AccentSeeds): Record<string, string> {
	return {
		'--accent-h': seeds.hue.toFixed(2),
		'--accent-c-light': seeds.light.color.c.toFixed(4),
		'--accent-c-dark': seeds.dark.color.c.toFixed(4),
		'--accent-l-light': seeds.light.color.l.toFixed(4),
		'--accent-l-dark': seeds.dark.color.l.toFixed(4),
		'--on-accent-light': cssOklch(onColor(seeds.light.on, seeds.hue)),
		'--on-accent-dark': cssOklch(onColor(seeds.dark.on, seeds.hue)),
	};
}

/** The custom properties a custom accent sets, every one removed when it is taken away. */
export const ACCENT_PROPERTIES = [
	'--accent-h',
	'--accent-c-light',
	'--accent-c-dark',
	'--accent-l-light',
	'--accent-l-dark',
	'--on-accent-light',
	'--on-accent-dark',
] as const;
