/**
 * The design system's themes, and the surfaces and tints an accent is drawn on, as `@riksarkivet/ui` exports them at the
 * app's pin (`THEMES`, `ACCENT_SURFACES`, `ACCENT_TINTS`). Spelled here because the browser suite's runner cannot load
 * TypeScript from under node_modules (Node refuses to strip its types); `src/lib/appearance-lists.test.ts` holds them
 * equal to the library's, so a pin that changes them fails until they are changed here too.
 */
export const THEME_IDS = ['ink', 'fjord', 'paper', 'moss', 'contrast'];

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
];

export const ACCENT_TINTS = [0.1];
