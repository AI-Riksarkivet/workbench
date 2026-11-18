/**
 * The boot script's source: the kept appearance's mode and theme on <html> before the first paint, so the page never
 * flashes another theme. The app builds it with `core.ts` into one classic same-origin script (`appearance-boot.js`,
 * its Vite plugin `appearanceBoot`), which the strict CSP admits with no hash. The accent needs the page's stylesheets
 * to be derived against, so the store sets it when the app starts, before the app draws anything that shows it.
 */
import { applyMode, browserKept, keptAppearance } from './core.js';

applyMode(
	document.documentElement,
	keptAppearance(browserKept()),
	matchMedia('(prefers-color-scheme: dark)').matches,
);
