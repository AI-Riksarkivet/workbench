import { expect, test } from '@playwright/test';
import { callsOf, person, signIn } from './session';

// A dataset with no workbench descriptor, in a source that is not a lakehouse: listed under its source as a
// plain table with what could not be opened, and shown in the dock as rows and one row, from its schema. What
// the table cannot do stays on screen, disabled, with the reason (`#lib/explore/pane-capabilities.ts`).

test('the dataset list groups the datasets by source and says what could not be read', async ({
	page,
	context,
}) => {
	await signIn(context, person('Edsger'));
	await page.goto('/datasets');

	const archive = page.getByRole('region', { name: 'Field archive' });
	await expect(archive).toContainText('Lance namespace');
	await expect(archive).toContainText('read-only');
	const survey = archive.getByRole('link', { name: /field\$survey/ });
	await expect(survey).toContainText('table');
	await expect(survey).toContainText('drafts could not be opened: the catalog lists it');
	await expect(archive.getByRole('list', { name: 'Not listed' })).toContainText(
		'sealed could not be read: catalog denied the request',
	);
	await expect(
		page.getByRole('list', { name: 'Datasets in The lakehouse' }).getByRole('link'),
	).toHaveCount(4);
});

test('a plain table pages, sorts, searches and ranks its rows, and opens one in full', async ({
	page,
	context,
}) => {
	const who = person('Grace');
	await signIn(context, who);
	await page.goto('/datasets/archive~field%24survey');

	const rows = page.getByRole('region', { name: 'Rows' });
	const table = rows.getByRole('table');
	await expect(table.getByRole('row')).toHaveCount(5);
	await expect(rows.getByRole('status').last()).toHaveText('1–4 of 4 rows');

	await rows.getByRole('button', { name: 'n', exact: true }).click();
	await rows.getByRole('button', { name: 'n', exact: true }).click();
	await expect(table.getByRole('row').nth(1)).toContainText('salt and grain');
	await expect(table.getByRole('columnheader', { name: /^n integer$/ })).toHaveAttribute(
		'aria-sort',
		'descending',
	);

	await rows.getByRole('searchbox').fill('salt');
	await rows.getByRole('searchbox').press('Enter');
	await expect(table.getByRole('row')).toHaveCount(3);

	await table.getByRole('row', { name: /salt in the harbour/ }).click();
	const row = page.getByRole('region', { name: /observations · row 0/ });
	await expect(row).toContainText('salt in the harbour');
	await row.getByRole('button', { name: 'Similar rows' }).click();
	await expect(rows.getByRole('status').last()).toHaveText('1–3 of 3 nearest rows');

	const calls = (await callsOf(page, who)).filter((c) => c.path.includes('/rows'));
	expect(calls.map((c) => new URLSearchParams(c.query ?? '').toString())).toEqual(
		expect.arrayContaining([
			'offset=0&limit=50&sort=n&order=desc',
			'offset=0&limit=50&q=salt',
			'offset=0&limit=50&near=0',
		]),
	);
});

test('what a plain table cannot do is disabled with its reason', async ({ page, context }) => {
	await signIn(context, person('Barbara'));
	await page.goto('/datasets/archive~field%24survey');

	// The dock opens the rows and the row alone; every view its columns could feed is offered to set up.
	await expect(page.getByRole('tab')).toHaveCount(2);
	await page.getByRole('button', { name: 'Open a panel' }).first().click();
	for (const view of ['Atlas', 'Graph', 'Tree']) {
		await expect(page.getByRole('menuitem', { name: new RegExp(`^${view}`) })).toHaveText(
			`${view} Set up…`,
		);
	}
	await page.keyboard.press('Escape');
	await expect(page.getByRole('menu')).toHaveCount(0);
	await page.getByRole('table').getByRole('row').nth(1).click();
	const send = page.getByRole('button', { name: 'Send to a project' });
	await expect(send).toHaveAccessibleDescription("source 'archive' is not marked annotatable");
	await send.focus();
	await expect(page.getByRole('status').filter({ hasText: /^Send to a project — / })).toHaveText(
		"Send to a project — source 'archive' is not marked annotatable",
	);
	await send.blur();
	await expect(page.getByRole('status').filter({ hasText: /^Send to a project — / })).toBeHidden();
	await send.hover();
	await expect(page.getByRole('status').filter({ hasText: /^Send to a project — / })).toBeVisible();
});
