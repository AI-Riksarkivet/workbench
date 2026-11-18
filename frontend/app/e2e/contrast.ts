import type { Locator, Page } from '@playwright/test';

/** WCAG 2.2's contrast ratio of an element's text on what is painted behind it: its own background composited over
 *  the first opaque surface beneath it. Every colour is resolved by the browser itself (a canvas paints the computed
 *  CSS colour). */
export function textContrast(target: Locator): Promise<number> {
	return target.evaluate((el: HTMLElement) => {
		const context = document
			.createElement('canvas')
			.getContext('2d', { willReadFrequently: true })!;
		const rgba = (css: string): [number, number, number, number] => {
			context.clearRect(0, 0, 1, 1);
			context.fillStyle = css;
			context.fillRect(0, 0, 1, 1);
			const [r = 0, g = 0, b = 0, a = 0] = context.getImageData(0, 0, 1, 1).data;
			return [r, g, b, a / 255];
		};
		let surface: HTMLElement | null = el.parentElement;
		let behind = rgba('white');
		while (surface) {
			const color = rgba(getComputedStyle(surface).backgroundColor);
			if (color[3] === 1) {
				behind = color;
				break;
			}
			surface = surface.parentElement;
		}
		const [hr, hg, hb, ha] = rgba(getComputedStyle(el).backgroundColor);
		const over = (front: number, back: number): number => front * ha + back * (1 - ha);
		const background = [over(hr, behind[0]), over(hg, behind[1]), over(hb, behind[2])];
		const text = rgba(getComputedStyle(el).color).slice(0, 3);
		const luminance = (rgb: number[]): number => {
			const [r = 0, g = 0, b = 0] = rgb.map((c) => {
				const v = c / 255;
				return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
			});
			return 0.2126 * r + 0.7152 * g + 0.0722 * b;
		};
		const [hi, lo] = [luminance(text), luminance(background)].sort((x, y) => y - x);
		return ((hi ?? 0) + 0.05) / ((lo ?? 0) + 0.05);
	});
}

/** What the accent measures on the page in light and in dark: its least contrast as text on any of `surfaces` (token
 *  names) and on its own tints over each, which surface that was, and the least of the text drawn on it at rest and
 *  hovered. Every colour is the one the browser paints (a canvas paints the computed CSS colour). */
export function accentOnSurfaces(
	page: Page,
	surfaces: string[],
	tints: number[],
): Promise<Record<'light' | 'dark', { onSurfaces: number; worst: string; textOnAccent: number }>> {
	return page.evaluate(
		({ surfaces, tints }) => {
			const context = document
				.createElement('canvas')
				.getContext('2d', { willReadFrequently: true })!;
			const probe = document.createElement('span');
			document.body.append(probe);
			const token = (name: string): number[] => {
				probe.style.color = `var(${name})`;
				context.clearRect(0, 0, 1, 1);
				context.fillStyle = getComputedStyle(probe).color;
				context.fillRect(0, 0, 1, 1);
				return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3));
			};
			const lum = (rgb: number[]) => {
				const [r = 0, g = 0, b = 0] = rgb.map((v) => {
					const c = v / 255;
					return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
				});
				return 0.2126 * r + 0.7152 * g + 0.0722 * b;
			};
			const ratio = (a: number[], b: number[]) => {
				const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
				return ((x ?? 0) + 0.05) / ((y ?? 0) + 0.05);
			};
			const over = (front: number[], alpha: number, back: number[]) =>
				front.map((v, i) => Math.round(v * alpha + (back[i] ?? 0) * (1 - alpha)));
			const root = document.documentElement;
			const wasDark = root.classList.contains('dark');
			const out: Record<string, { onSurfaces: number; worst: string; textOnAccent: number }> = {};
			for (const mode of ['light', 'dark'] as const) {
				root.classList.toggle('dark', mode === 'dark');
				const primary = token('--primary');
				const on = token('--primary-foreground');
				let least = Infinity;
				let worst = '';
				for (const surface of surfaces) {
					const back = token(surface);
					for (const [name, ground] of [
						[surface, back],
						...tints.map((alpha) => [
							`${surface} under a ${alpha * 100}% tint`,
							over(primary, alpha, back),
						]),
					] as [string, number[]][]) {
						const r = ratio(primary, ground);
						if (r < least) [least, worst] = [r, name];
					}
				}
				const textOnAccent = Math.min(ratio(on, primary), ratio(on, token('--primary-hover')));
				out[mode] = {
					onSurfaces: Math.round(least * 100) / 100,
					worst,
					textOnAccent: Math.round(textOnAccent * 100) / 100,
				};
			}
			root.classList.toggle('dark', wasDark);
			probe.remove();
			return out as Record<
				'light' | 'dark',
				{ onSurfaces: number; worst: string; textOnAccent: number }
			>;
		},
		{ surfaces, tints },
	);
}
