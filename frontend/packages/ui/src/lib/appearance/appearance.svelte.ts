/**
 * The person's appearance, live: the kept choice (`core.ts`) put on <html>. The boot script already put its mode and
 * theme there before the first paint; `start()` takes over, derives the accent against the page's own surfaces and
 * follows the system's light or dark when the mode is `system`. Storage is best-effort: a browser that refuses it still
 * gets the choice for this visit.
 */
import { untrack } from 'svelte';
import {
	ACCENT_PROPERTIES,
	ACCENT_SURFACES,
	accentVars,
	chosenColour,
	deriveAccent,
	type AccentSeeds,
	type Surfaces,
} from './appearance.js';
import type { Rgb } from './color.js';
import {
	applyMode,
	browserKept,
	DEFAULT_APPEARANCE,
	isAccent,
	isDark,
	keptAppearance,
	writeAppearance,
	type Appearance,
} from './core.js';

const DARK_QUERY = '(prefers-color-scheme: dark)';

/** A CSS colour as the page paints it: the browser's own 2-D canvas converts whatever syntax the token holds. */
function painted(context: CanvasRenderingContext2D, css: string): Rgb {
	context.clearRect(0, 0, 1, 1);
	context.fillStyle = '#000';
	context.fillStyle = css;
	context.fillRect(0, 0, 1, 1);
	const [r = 0, g = 0, b = 0] = context.getImageData(0, 0, 1, 1).data;
	return [r, g, b];
}

/**
 * The surfaces a theme paints in light and in dark, MEASURED from the tokens: a hidden probe carries the theme and the
 * mode (`.light` / `.dark`, which `tokens.css` and `themes.css` declare on any element, not only <html>) and, for a
 * custom accent, its hue and chroma, since the tinted surfaces follow them. Nothing about a surface is repeated here.
 */
export function measureSurfaces(
	theme: Appearance['theme'],
	accent: { hue: number; light: number; dark: number } | null,
): Surfaces {
	const context = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
	if (!context) return { light: [], dark: [] };
	const probe = document.createElement('div');
	probe.hidden = true;
	probe.dataset['theme'] = theme;
	if (accent) {
		probe.style.setProperty('--accent-h', accent.hue.toFixed(2));
		probe.style.setProperty('--accent-c-light', accent.light.toFixed(4));
		probe.style.setProperty('--accent-c-dark', accent.dark.toFixed(4));
	}
	const swatch = document.createElement('span');
	probe.append(swatch);
	document.body.append(probe);
	const read = (mode: 'light' | 'dark'): Rgb[] => {
		probe.className = mode;
		return ACCENT_SURFACES.map((token) => {
			swatch.style.color = `var(${token})`;
			return painted(context, getComputedStyle(swatch).color);
		});
	};
	const surfaces = { light: read('light'), dark: read('dark') };
	probe.remove();
	return surfaces;
}

/**
 * A colour's accent on a theme, against what that theme paints. The tinted surfaces follow the accent's chroma, which
 * the gamut trims at the lightness the correction settles on, so the surfaces are measured again with the chroma just
 * derived until nothing moves (two or three rounds).
 */
function deriveOnPage(theme: Appearance['theme'], accent: string): AccentSeeds | null {
	const chosen = chosenColour(accent);
	if (!chosen) return null;
	let tint = { hue: chosen.h, light: chosen.c, dark: chosen.c };
	let seeds: AccentSeeds | null = null;
	for (let round = 0; round < 4; round++) {
		seeds = deriveAccent(accent, measureSurfaces(theme, tint));
		if (!seeds) return null;
		const next = { hue: seeds.hue, light: seeds.light.color.c, dark: seeds.dark.color.c };
		if (Math.abs(next.light - tint.light) < 1e-4 && Math.abs(next.dark - tint.dark) < 1e-4) break;
		tint = next;
	}
	return seeds;
}

class AppearanceState {
	#choice = $state<Appearance>({ ...DEFAULT_APPEARANCE });
	#systemDark = $state(false);
	#seeds = $state.raw<AccentSeeds | null>(null);
	#revision = $state(0);

	get theme(): Appearance['theme'] {
		return this.#choice.theme;
	}
	get mode(): Appearance['mode'] {
		return this.#choice.mode;
	}
	get accent(): string | null {
		return this.#choice.accent;
	}
	/** Whether the page is dark now. */
	get dark(): boolean {
		return isDark(this.#choice.mode, this.#systemDark);
	}
	/** The custom accent as it is drawn, with what was corrected for contrast; null for the theme's own. */
	get accentSeeds(): AccentSeeds | null {
		return this.#seeds;
	}
	/**
	 * What the page paints, resolved: changes whenever its mode, theme or accent does, after <html> carries it. A canvas
	 * that clears to a token or colours with one reads this to know when to read them again.
	 */
	get resolved(): {
		dark: boolean;
		theme: Appearance['theme'];
		accent: string | null;
		revision: number;
	} {
		return { dark: this.dark, theme: this.theme, accent: this.accent, revision: this.#revision };
	}

	/** Takes over from the boot script; returns the stop. Call once, in the browser. It tracks nothing, so an effect
	 *  that calls it runs once rather than on every choice it then reads. */
	start(): () => void {
		return untrack(() => {
			this.#choice = keptAppearance(browserKept());
			const query = matchMedia(DARK_QUERY);
			this.#systemDark = query.matches;
			this.#apply(false);
			const follow = (event: MediaQueryListEvent): void => {
				this.#systemDark = event.matches;
				if (this.#choice.mode === 'system') this.#apply(true);
			};
			query.addEventListener('change', follow);
			return () => query.removeEventListener('change', follow);
		});
	}

	/** Changes part of the choice, keeps it, and shows it. An accent that is not a strict `#rrggbb` is refused. */
	set(change: Partial<Appearance>): void {
		const accent =
			change.accent === undefined ? this.#choice.accent : (change.accent?.toLowerCase() ?? null);
		if (accent !== null && !isAccent(accent)) return;
		this.#choice = { ...this.#choice, ...change, accent };
		browserKept().write(writeAppearance(this.#choice));
		this.#apply(true);
	}

	/** What a colour would become on the current theme, for a preview; null when it is not a strict `#rrggbb`. */
	preview(hex: string): AccentSeeds | null {
		const accent = hex.trim().toLowerCase();
		return isAccent(accent) ? deriveOnPage(this.#choice.theme, accent) : null;
	}

	#apply(switching: boolean): void {
		const root = document.documentElement;
		// Nothing animates its colours across a switch: the page changes at once instead of every surface easing.
		if (switching) root.classList.add('appearance-switching');
		applyMode(root, this.#choice, this.#systemDark);
		for (const name of ACCENT_PROPERTIES) root.style.removeProperty(name);
		const accent = this.#choice.accent;
		const seeds = accent ? deriveOnPage(this.#choice.theme, accent) : null;
		if (seeds)
			for (const [name, value] of Object.entries(accentVars(seeds)))
				root.style.setProperty(name, value);
		this.#seeds = seeds;
		this.#revision = untrack(() => this.#revision) + 1;
		if (switching) {
			requestAnimationFrame(() =>
				requestAnimationFrame(() => root.classList.remove('appearance-switching')),
			);
		}
	}
}

export const appearance = new AppearanceState();
