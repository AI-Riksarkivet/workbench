/**
 * The ONE definition of a kept appearance, shared by the app's store (`appearance.svelte.ts`) and the boot script the
 * app builds from `boot.ts` (the app's Vite plugin `appearanceBoot`), so the two can never read a record differently.
 * It imports nothing, so the boot script is this file and a few lines.
 *
 * A record is `{ v: 2, theme, mode, accent }`: a shipped theme's id, a mode, and the accent as one strict `#rrggbb` or
 * null. Nothing else is read from it: the custom properties a colour amounts to are derived from it on the page, never
 * stored, so a tampered or older record can set no token. A record that is anything else is read field by field (an
 * unknown field falls back to its default) and written back clean.
 */
export const THEMES = [
	{ id: 'ink', label: 'Ink', description: 'Neutral greys, a blue-violet accent' },
	{ id: 'fjord', label: 'Fjord', description: 'Cool blue-grey, a sea-glass teal' },
	{ id: 'paper', label: 'Paper', description: 'Warm paper, a sealing-wax red' },
	{ id: 'moss', label: 'Moss', description: 'Green-grey, a forest green' },
	{ id: 'contrast', label: 'Contrast', description: 'Black and white, 7:1 text' },
] as const;

export type ThemeId = (typeof THEMES)[number]['id'];
export type Mode = 'light' | 'dark' | 'system';

export type Appearance = {
	theme: ThemeId;
	mode: Mode;
	/** A chosen accent as `#rrggbb`, or null for the theme's own. */
	accent: string | null;
};

export const STORAGE_KEY = 'workbench-appearance';
/** A first visit is dark, as the app has always opened. */
export const DEFAULT_APPEARANCE: Appearance = { theme: 'ink', mode: 'dark', accent: null };

const MODES: readonly string[] = ['light', 'dark', 'system'];
const isTheme = (v: unknown): v is ThemeId => THEMES.some((t) => t.id === v);
const isMode = (v: unknown): v is Mode => typeof v === 'string' && MODES.includes(v);
/** Strictly `#rrggbb`, lower case: the one shape an accent is kept in. */
export const isAccent = (v: unknown): v is string =>
	typeof v === 'string' && /^#[0-9a-f]{6}$/.test(v);

/** A kept record read tolerantly: each field it cannot use falls back to its default. */
export function readAppearance(raw: string | null): Appearance {
	let value: unknown = null;
	try {
		value = raw === null ? null : JSON.parse(raw);
	} catch {
		value = null;
	}
	const record =
		typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {};
	return {
		theme: isTheme(record['theme']) ? record['theme'] : DEFAULT_APPEARANCE.theme,
		mode: isMode(record['mode']) ? record['mode'] : DEFAULT_APPEARANCE.mode,
		accent: isAccent(record['accent']) ? record['accent'] : null,
	};
}

/** The record kept for a choice: its fields, and nothing else. */
export const writeAppearance = (choice: Appearance): string =>
	JSON.stringify({ v: 2, theme: choice.theme, mode: choice.mode, accent: choice.accent });

export const isDark = (mode: Mode, systemDark: boolean): boolean =>
	mode === 'dark' || (mode === 'system' && systemDark);

/** Puts a choice's mode and theme on the root element. The accent is the store's to derive, on the page. */
export function applyMode(root: HTMLElement, choice: Appearance, systemDark: boolean): void {
	const dark = isDark(choice.mode, systemDark);
	root.classList.toggle('dark', dark);
	root.style.colorScheme = dark ? 'dark' : 'light';
	root.dataset['theme'] = choice.theme;
}

/** What the browser keeps, read and written best-effort: a browser that refuses storage still gets the default. */
export type Kept = { read: () => string | null; write: (value: string) => void };

/** Reads the kept choice, rewrites it clean when it was not, and returns it. */
export function keptAppearance(kept: Kept): Appearance {
	const raw = kept.read();
	const choice = readAppearance(raw);
	const clean = writeAppearance(choice);
	if (raw !== null && raw !== clean) kept.write(clean);
	return choice;
}

/** `localStorage` under `STORAGE_KEY`, every access guarded. */
export const browserKept = (): Kept => ({
	read: () => {
		try {
			return localStorage.getItem(STORAGE_KEY);
		} catch {
			return null;
		}
	},
	write: (value) => {
		try {
			localStorage.setItem(STORAGE_KEY, value);
		} catch {
			// Kept for this visit only.
		}
	},
});
