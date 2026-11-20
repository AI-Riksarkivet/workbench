import { expect, test } from '@playwright/test';
import { person, signIn } from './session';

// The app's shell on the built app: its one navigation, the rail. Run in the `webgpu` project (`playwright.config.ts`),
// Chromium's new headless mode, where page transitions run as in a person's browser: it is across one that a rail row's
// tooltip came back, and the headless shell runs none.

test('a rail row’s tooltip does not open over the page its click went to, under the pointer resting on it', async ({
	page,
	context,
}) => {
	await signIn(context, person('Annotator'));
	await page.goto('/projects/sorting');
	const rail = page.getByRole('navigation', { name: 'Pages', exact: true });
	// The rail as icons, where its rows' names are tooltips (it opens so at this width unless the reader widened it).
	await expect(page.locator('[data-slot="sidebar"][data-state="collapsed"]')).toHaveCount(1);

	await rail.getByRole('link', { name: 'Datasets' }).click();
	await expect(page).toHaveURL(/\/datasets/);
	// The tooltip a hover opens does so on a delay: it is read once that has long passed, the pointer not moved.
	await page.waitForTimeout(1500);

	const shown = page.locator('[data-slot="tooltip-content"]').filter({ visible: true });
	await expect(shown).toHaveCount(0);
	// Once the pointer moves on, the rail's names show again on a hover.
	await page.mouse.move(600, 400);
	await rail.getByRole('link', { name: 'Datasets' }).hover();
	await expect(shown).toHaveText('Datasets');
});
