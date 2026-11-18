/**
 * OKLCH ↔ sRGB and the WCAG 2.2 contrast ratio, enough to derive an accent from one chosen colour and prove it
 * readable on the surfaces the page actually paints. Björn Ottosson's OKLab matrices; sRGB's transfer function from
 * IEC 61966-2-1.
 */

/** A colour in OKLCH: lightness 0–1, chroma ≥ 0, hue in degrees. */
export type Oklch = { l: number; c: number; h: number };
/** An sRGB colour as the page paints it, each channel 0–255. */
export type Rgb = readonly [number, number, number];

const toLinear = (v: number): number => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
const toGamma = (v: number): number =>
	v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055;

function oklchToLinear({ l, c, h }: Oklch): [number, number, number] {
	const rad = (h * Math.PI) / 180;
	const a = c * Math.cos(rad);
	const b = c * Math.sin(rad);
	const l_ = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3;
	const m_ = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3;
	const s_ = (l - 0.0894841775 * a - 1.291485548 * b) ** 3;
	return [
		4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
		-1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
		-0.0041960863 * l_ - 0.7034186147 * m_ + 1.707614701 * s_,
	];
}

function inGamut(color: Oklch): boolean {
	const eps = 1e-6;
	return oklchToLinear(color).every((v) => v >= -eps && v <= 1 + eps);
}

/** The most chroma, up to `c`, the colour's lightness and hue can carry inside sRGB. */
export function clampChroma(color: Oklch): Oklch {
	if (inGamut(color)) return color;
	let lo = 0;
	let hi = color.c;
	for (let i = 0; i < 24; i++) {
		const mid = (lo + hi) / 2;
		if (inGamut({ ...color, c: mid })) lo = mid;
		else hi = mid;
	}
	return { ...color, c: lo };
}

/** The colour as the page paints it, clipped into sRGB. */
export function oklchToRgb(color: Oklch): Rgb {
	const [r = 0, g = 0, b = 0] = oklchToLinear(color).map((v) =>
		Math.round(toGamma(Math.min(1, Math.max(0, v))) * 255),
	);
	return [r, g, b];
}

function rgbToOklch(rgb: Rgb): Oklch {
	const [r, g, b] = rgb.map((v) => toLinear(v / 255)) as [number, number, number];
	const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
	const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
	const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
	const L = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
	const A = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
	const B = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
	const c = Math.hypot(A, B);
	const h = c < 1e-4 ? 0 : ((Math.atan2(B, A) * 180) / Math.PI + 360) % 360;
	return { l: L, c, h };
}

/** WCAG 2.2 contrast ratio of two sRGB colours, 1–21. */
export function contrastRgb(x: Rgb, y: Rgb): number {
	const luminance = (rgb: Rgb) => {
		const [r, g, b] = rgb.map((v) => toLinear(v / 255)) as [number, number, number];
		return 0.2126 * r + 0.7152 * g + 0.0722 * b;
	};
	const [hi, lo] = [luminance(x), luminance(y)].sort((p, q) => q - p);
	return ((hi ?? 0) + 0.05) / ((lo ?? 0) + 0.05);
}

/** WCAG 2.2 contrast ratio of two colours, 1–21, each as the page paints it. */
export const contrast = (x: Oklch, y: Oklch): number => contrastRgb(oklchToRgb(x), oklchToRgb(y));

/** `front` painted at `alpha` over `back`, as the browser composites it. */
export const over = (front: Rgb, alpha: number, back: Rgb): Rgb => [
	Math.round(front[0] * alpha + back[0] * (1 - alpha)),
	Math.round(front[1] * alpha + back[1] * (1 - alpha)),
	Math.round(front[2] * alpha + back[2] * (1 - alpha)),
];

/** `#rgb` or `#rrggbb` as OKLCH; null for anything else. */
export function hexToOklch(hex: string): Oklch | null {
	const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
	if (!match?.[1]) return null;
	const digits = match[1].length === 3 ? [...match[1]].map((d) => d + d).join('') : match[1];
	const [r = 0, g = 0, b = 0] = [0, 2, 4].map((i) => parseInt(digits.slice(i, i + 2), 16));
	return rgbToOklch([r, g, b]);
}

/** The colour as `#rrggbb`, clipped into sRGB. */
export const oklchToHex = (color: Oklch): string =>
	`#${oklchToRgb(color)
		.map((v) => v.toString(16).padStart(2, '0'))
		.join('')}`;

/** The colour as a CSS `oklch()` with a precision that round-trips. */
export function cssOklch({ l, c, h }: Oklch): string {
	return `oklch(${l.toFixed(4)} ${c.toFixed(4)} ${h.toFixed(2)})`;
}
