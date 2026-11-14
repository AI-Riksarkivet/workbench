/**
 * A theme token as the `#rrggbb` the WebGPU renderers clear their canvas to: they take a hex colour,
 * and the tokens are OKLCH. The browser's own 2-D canvas converts it, so any CSS colour the token holds
 * converts the same way the page paints it.
 */
function cssColorHex(css: string): string {
	const context = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
	if (!context) return '#000000';
	context.fillStyle = '#000000';
	context.fillStyle = css;
	context.fillRect(0, 0, 1, 1);
	const [r = 0, g = 0, b = 0] = context.getImageData(0, 0, 1, 1).data;
	return `#${[r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')}`;
}

/** The page's `--background` token, as the theme now paints it. */
export function themeBackground(): string {
	const token = getComputedStyle(document.documentElement).getPropertyValue('--background').trim();
	return cssColorHex(token || '#000000');
}
