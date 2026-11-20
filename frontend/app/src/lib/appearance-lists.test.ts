import { describe, expect, it } from 'vitest';
import { ACCENT_SURFACES, ACCENT_TINTS, THEMES } from '@riksarkivet/ui/appearance';
import * as spelled from '../../e2e/appearance-lists.ts';

describe("the browser suite's copy of the design system's appearance lists", () => {
	it('is the library at its pin: every theme, surface and tint the accent contrast check walks', () => {
		expect({
			themes: spelled.THEME_IDS,
			surfaces: spelled.ACCENT_SURFACES,
			tints: spelled.ACCENT_TINTS,
		}).toEqual({
			themes: THEMES.map((theme) => theme.id),
			surfaces: [...ACCENT_SURFACES],
			tints: [...ACCENT_TINTS],
		});
	});
});
