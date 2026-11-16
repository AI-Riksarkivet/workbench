import type { Locator } from '@playwright/test';

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
