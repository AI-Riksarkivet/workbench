import type { AppearanceOptions } from '@riksarkivet/ui/appearance';

/**
 * Where the person's appearance (theme, mode, accent) is kept, and what a first visit gets: named once, for the design
 * system's Vite plugin (the boot script `/appearance-boot.js`, `vite.config.ts`) and for the store the root layout
 * starts, so the two read one record. The key is the one the workbench has always kept the choice under, so a browser
 * that holds it keeps its theme; a first visit is dark, as the app has always opened.
 */
export const APPEARANCE: AppearanceOptions = {
	storageKey: 'workbench-appearance',
	defaults: { theme: 'ink', mode: 'dark' },
};
