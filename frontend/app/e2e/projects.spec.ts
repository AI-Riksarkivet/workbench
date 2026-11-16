import { expect, test, type BrowserContext, type Page } from '@playwright/test';
import { callsOf, person, signIn, watchCsp, type Call } from './session';
import { EDGE } from './ports';
import type { MockIdentity } from './mock-oidc';

// The project plane on the built app: the projects of the reader's organisations, one project's queue, review,
// settings, members and publish, and sending a search's rows to a project. A person's role on the seeded projects is
// the first word of their name (`e2e/mock-projects.ts`), and the mock annotator refuses as the service does.

async function as(context: BrowserContext, name: string): Promise<MockIdentity> {
	const who = person(name);
	await signIn(context, who);
	return who;
}

/** The annotator calls one person caused, by method and path (no query). */
async function annotatorCalls(page: Page, who: MockIdentity): Promise<Call[]> {
	return (await callsOf(page, who)).filter((call) => call.service === 'annotator');
}

const letters = (page: Page) => page.goto('/projects/letters');
const row = (page: Page, key: string) => page.getByRole('row').filter({ hasText: key });

test('the projects of the reader’s organisation are listed with their state and progress, every call proven', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	const violations = await watchCsp(page);

	await page.goto('/projects');

	await expect(page.getByRole('heading', { level: 1, name: 'Projects' })).toBeVisible();
	const list = page.getByRole('list', { name: 'Projects of acme' });
	await expect(
		list.getByRole('link', { name: /Letters.*labeling.*1\/4 items finished/s }),
	).toBeVisible();
	await expect(list.getByRole('link', { name: /Charters.*frozen/s })).toBeVisible();
	await expect(page).toHaveTitle('Projects · workbench');
	// Every in-app link carries the project it opens as the reader's (`?project=`).
	await expect(list.getByRole('link', { name: /Letters/ })).toHaveAttribute(
		'href',
		'/projects/letters?project=letters',
	);
	const calls = await annotatorCalls(page, who);
	expect(calls).toContainEqual({
		service: 'annotator',
		method: 'GET',
		path: '/projects',
		status: 200,
	});
	expect((await callsOf(page, who)).filter((call) => call.refused !== undefined)).toEqual([]);
	expect(await violations()).toEqual([]);
});

test('a person in no organisation is told so, and nothing is listed for them', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Stranger');

	await page.goto('/projects');

	await expect(page.getByText('You belong to no organisation.')).toBeVisible();
	await expect(page.getByRole('button', { name: 'New project' })).toBeDisabled();
	expect((await annotatorCalls(page, who)).map((call) => call.path)).toEqual(['/me']);
});

test('a failed list says what failed and tries again', async ({ page, context }) => {
	const who = await as(context, 'Viewer');
	await page.request.post(
		`${EDGE}/api/viewer/__fault?sub=${encodeURIComponent(who.sub)}&path=/projects&kind=fail`,
	);

	await page.goto('/projects');
	await expect(page.getByRole('alert')).toHaveText('The projects of acme could not be read.');
	await expect(
		page.getByText('Details: the annotator could not reach its state store'),
	).toBeVisible();
	await page.request.post(
		`${EDGE}/api/viewer/__fault?sub=${encodeURIComponent(who.sub)}&path=/projects&kind=clear`,
	);
	await page.getByRole('button', { name: 'Try again' }).click();

	await expect(page.getByRole('link', { name: /Letters/ })).toBeVisible();
});

test('a project is created from a template, its whole ontology in one document', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await page.goto('/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	await page.getByLabel('Slug').fill('portraits');
	await page.getByLabel('Title').fill('Portraits');
	const posted = page.waitForRequest(
		(req) => req.method() === 'POST' && req.url().endsWith('/api/annotator/projects'),
	);

	await page.getByRole('button', { name: 'Create project' }).click();

	const body = (await posted).postDataJSON() as { tenant: string; slug: string; title: string };
	expect({ tenant: body.tenant, slug: body.slug, title: body.title }).toEqual({
		tenant: 'acme',
		slug: 'portraits',
		title: 'Portraits',
	});
	await expect(page.getByRole('link', { name: /Portraits.*draft/s })).toBeVisible();
});

test('the queue offers the canvas’s events, claim, submit and fix & accept, and opens each item to annotate or to view', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await letters(page);
	await expect(row(page, 'letter-001/0').getByRole('button', { name: /^Claim/ })).toBeVisible();
	await expect(row(page, 'letter-001/1').getByRole('button', { name: /^Submit/ })).toBeVisible();
	await expect(
		row(page, 'letter-002/0').getByRole('button', { name: /^Fix & accept/ }),
	).toBeVisible();
	// The item the reader holds opens to annotate; one nobody holds opens to view.
	await expect(
		row(page, 'letter-001/1').getByRole('link', { name: 'Annotate letter-001/1' }),
	).toHaveAttribute('href', '/projects/letters/tasks/letters-t2?project=letters');
	await expect(
		row(page, 'letter-001/0').getByRole('link', { name: 'View letter-001/0' }),
	).toHaveAttribute('href', '/projects/letters/tasks/letters-t1?project=letters');
});

test('an annotator is offered no review of others’ work', async ({ page, context }) => {
	await as(context, 'Annotator');
	await letters(page);

	await expect(page.getByTestId('reader-role')).toHaveText(
		'You annotate in this project: you claim items and label them.',
	);
	await expect(
		row(page, 'letter-002/0').getByRole('button', { name: /Accept|Request changes/ }),
	).toHaveCount(0);
});

test('a reviewer requests changes with a note, and the note travels with the item', async ({
	page,
	context,
}) => {
	await as(context, 'Reviewer');
	await letters(page);

	await row(page, 'letter-002/0').getByRole('button', { name: 'Request changes' }).click();
	await page.getByRole('dialog').getByRole('textbox').fill('The stamp is unlabelled.');
	await page.getByRole('dialog').getByRole('button', { name: 'Request changes' }).click();

	await expect(row(page, 'letter-002/0')).toContainText('changes requested');
	await expect(row(page, 'letter-002/0')).toContainText('1 note');
});

test('a viewer is offered no transition of the project, no publish, and not who holds it', async ({
	page,
	context,
}) => {
	await as(context, 'Viewer');
	await letters(page);

	await expect(page.getByTestId('reader-role')).toHaveText(
		'You can view this project; your role does not let you change it.',
	);
	await expect(page.getByRole('button', { name: /Freeze|Send items/ })).toHaveCount(0);
	await page.getByTestId('tab-settings').click();
	await expect(page.getByTestId('members-not-shown')).toBeVisible();
	await page.getByTestId('tab-publish').click();
	await expect(page.getByRole('button', { name: 'Publish…' })).toBeDisabled();
	await expect(page.getByText('Only a manager of the project publishes it.')).toBeVisible();
});

test('a manager freezes and publishes a project whose items are all finished', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Manager');
	await page.goto('/projects/charters');
	await page.getByTestId('tab-publish').click();
	await page.getByRole('button', { name: 'Publish…' }).click();
	const sent = page.waitForRequest(
		(r) => r.method() === 'POST' && new URL(r.url()).pathname.endsWith('/projects/charters/events'),
	);

	await page
		.getByRole('dialog')
		.getByRole('button', { name: /Publish/ })
		.click();

	await expect(page.getByRole('heading', { level: 1, name: /Charters\s*published/ })).toBeVisible();
	// No target named, none sent: the service publishes into its default namespace of the project's own warehouse.
	expect((await sent).postDataJSON()).toEqual({ event: 'publish' });
	expect(await annotatorCalls(page, who)).toContainEqual({
		service: 'annotator',
		method: 'POST',
		path: '/projects/charters/events',
		status: 200,
	});
});

test('a manager edits the labels while labeling, and the edit is gone once the project is frozen', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await letters(page);
	await page.getByTestId('tab-settings').click();
	await expect(page.getByTestId('label-taxonomy')).toContainText('letter');
	await expect(page.getByTestId('edit-ontology-trigger')).toBeVisible();

	await page.getByRole('button', { name: 'Freeze' }).click();

	await expect(page.getByRole('heading', { level: 1, name: /Letters\s*frozen/ })).toBeVisible();
	await page.getByTestId('tab-settings').click();
	await expect(page.getByTestId('edit-ontology-trigger')).toHaveCount(0);
});

test('a manager grants a role, and revoking the last owner is refused in the door’s words', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Owner');
	await letters(page);
	await page.getByTestId('tab-settings').click();
	const members = page.getByTestId('members-panel');
	await members.getByRole('button', { name: 'Show who has access' }).click();
	const me = who.sub.replace(/^user:/, '');
	await expect(members.getByTestId('member-row')).toHaveCount(1);

	await members.getByLabel('Person to grant').fill('kc~fay');
	await members.getByTestId('grant-member').click();
	await expect(members.getByTestId('member-row')).toHaveCount(2);
	await members.getByRole('button', { name: `Revoke owner from ${me}` }).click();

	await expect(members).toContainText('holds the only remaining owner on this project');
	await expect(members.getByTestId('member-row')).toHaveCount(2);
});

test('a project the reader cannot see reads the same whether it exists or not', async ({
	page,
	context,
}) => {
	await as(context, 'Stranger');

	await page.goto('/projects/letters');

	await expect(
		page.getByRole('heading', { level: 1, name: 'No project letters that you can see' }),
	).toBeVisible();
	await expect(page.getByRole('link', { name: 'Back to the projects' })).toBeVisible();
});

test('the rows a search answered are sent to a project, labelled, and appear in its queue', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await page.goto('/datasets/lake~acme%24letters?project=letters');
	await page.getByRole('searchbox').fill('harbour');
	await page.keyboard.press('Enter');
	await page.getByRole('button', { name: 'Send to a project' }).click();
	const dialog = page.getByTestId('send-to-project');
	await expect(dialog.getByTestId('send-project')).toHaveValue('letters');
	await dialog.getByLabel('Label every item as').selectOption('letter');
	const posted = page.waitForRequest((req) =>
		req.url().endsWith('/api/annotator/projects/letters/items'),
	);

	await dialog.getByRole('button', { name: /^Send \d+ items?$/ }).click();

	const items = (
		(await posted).postDataJSON() as {
			items: { source: { where: string; keys: string[] }; prediction: unknown }[];
		}
	).items;
	expect(items[0]).toMatchObject({
		source: { where: 'lake~acme$letters', dataset_version: 3 },
		prediction: [{ shape_type: 'tag', label: 'letter' }],
	});
	await expect(dialog.getByTestId('send-done')).toContainText(`Sent ${items.length} items.`);
	await dialog.getByRole('link', { name: 'Open the project' }).click();
	// The seeded item and the sent one share the key: two rows, the sent one with its suggested label.
	await expect(row(page, items[0]!.source.keys[0]!)).toHaveCount(2);
	expect((await callsOf(page, who)).filter((call) => call.refused !== undefined)).toEqual([]);
});

const panel = (page: Page) => page.goto('/projects/panel');

test('consensus: each replica wears its chip, and the create form sends how many annotators each item gets', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await panel(page);
	await expect(page.getByText('replica 1/2')).toHaveCount(2);
	await expect(page.getByText('replica 2/2')).toHaveCount(2);

	await page.goto('/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	await page.getByLabel('Slug').fill('panel-two');
	await page.getByLabel(/annotators per item/i).fill('3');
	const posted = page.waitForRequest(
		(req) => req.method() === 'POST' && req.url().endsWith('/api/annotator/projects'),
	);
	await page.getByRole('button', { name: 'Create project' }).click();

	expect(((await posted).postDataJSON() as { consensus_n: number }).consensus_n).toBe(3);
});

test('adjudication: a manager picks the canonical replica, the pick is PUT, and the queue marks it', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Manager');
	await panel(page);
	await page.getByTestId('tab-publish').click();
	const adjudication = page.getByTestId('adjudication-panel');

	await adjudication.getByRole('button', { name: 'Pick' }).first().click();

	await expect(adjudication.getByText('canonical')).toBeVisible();
	await expect(adjudication.getByRole('button', { name: 'Pick', exact: true })).toHaveCount(0);
	await expect(adjudication.getByRole('button', { name: 'Re-pick' })).toHaveCount(1);
	await page.getByTestId('tab-labeling').click();
	const replica = (key: string, by: string) => row(page, key).filter({ hasText: by });
	await expect(replica('letter-001/0', 'user:alpha').getByText('canonical')).toBeVisible();
	await expect(replica('letter-001/0', 'user:beta').getByText('canonical')).toHaveCount(0);
	expect(await annotatorCalls(page, who)).toContainEqual({
		service: 'annotator',
		method: 'PUT',
		path: '/projects/panel/adjudications/panel-g1',
		status: 200,
	});
});

test('assign names a recipient, and the item is theirs', async ({ page, context }) => {
	await as(context, 'Manager');
	await letters(page);

	await row(page, 'letter-001/0').getByRole('button', { name: 'Assign…' }).click();
	await page.getByRole('dialog').getByRole('textbox').fill('kc~fay');
	await page.getByRole('dialog').getByRole('button', { name: 'Assign', exact: true }).click();

	await expect(row(page, 'letter-001/0')).toContainText('claimed');
	await expect(row(page, 'letter-001/0')).toContainText('kc~fay');
});

test('a bulk accept fires one gated event per item, and a partial failure names what failed and what landed', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await panel(page);
	const replicas = row(page, 'letter-002/0');
	await replicas.nth(0).getByRole('checkbox').check();
	await replicas.nth(1).getByRole('checkbox').check();

	await page.getByTestId('bulk-accept').click();

	await expect(
		page.getByText(/^1 of 2 — .*a reviewer may not review their own submission$/),
	).toBeVisible();
	await expect(replicas.filter({ hasText: 'user:alpha' })).toContainText('accepted');
	await expect(replicas.filter({ hasNotText: 'user:alpha' })).toContainText('in review');
});

test('annotations made elsewhere are imported into the item the reader holds', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await letters(page);

	await row(page, 'letter-001/1')
		.locator('input[type=file]')
		.setInputFiles({
			name: 'shapes.arrow',
			mimeType: 'application/vnd.apache.arrow.stream',
			buffer: Buffer.from('ARROW'),
		});

	await expect(row(page, 'letter-001/1').getByTestId('import-done')).toHaveText('1 imported');
	expect(await annotatorCalls(page, who)).toContainEqual({
		service: 'annotator',
		method: 'POST',
		path: '/tasks/letters-t2/import',
		status: 200,
	});
});

test('a failed publish says where it failed and why, and a retry publishes', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await page.goto('/projects/minutes');
	await page.getByTestId('tab-publish').click();
	await expect(
		page.getByText('the lakehouse refused the table: the namespace is protected'),
	).toBeVisible();
	await expect(page.getByText('at: creating the table')).toBeVisible();

	await page.getByRole('button', { name: 'Retry publish' }).click();

	await expect(page.getByRole('heading', { level: 1, name: /Minutes\s*published/ })).toBeVisible();
});

test('a lease that has lapsed reads as expired, never as held', async ({ page, context }) => {
	await as(context, 'Manager');
	await panel(page);

	await expect(row(page, 'map-003/0')).toContainText(/expired/i);
});

test('task YAML that does not parse names each error once and blocks the create', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await page.goto('/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	await page.getByLabel('Slug').fill('broken');
	await page.getByRole('button', { name: 'Task template' }).click();
	await page.getByRole('option', { name: 'custom (define in YAML)' }).click();

	await page
		.getByRole('textbox', { name: /YAML/i })
		.fill('labels:\n  - name: a\n    draw: [nope, nope]\n');

	const errors = page.getByTestId('task-yaml-errors').getByRole('listitem');
	await expect(errors).toHaveText([
		'labels[0].draw: `nope` is not one of bbox, polygon, mask, segment',
	]);
	await expect(page.getByRole('button', { name: 'Create project' })).toBeDisabled();
});

test('a viewer is offered no project to send to: the list is the projects the store lets them send items to', async ({
	page,
	context,
}) => {
	await as(context, 'Viewer');
	await page.goto('/datasets/lake~acme%24letters?project=letters');
	await page.getByRole('searchbox').fill('harbour');
	await page.keyboard.press('Enter');
	await page.getByRole('button', { name: 'Send to a project' }).click();
	const dialog = page.getByTestId('send-to-project');

	await expect(dialog.getByText('No project is taking items: create one below.')).toBeVisible();
	await expect(dialog.getByTestId('send-project').getByRole('option')).toHaveCount(1);
	await expect(dialog.getByRole('button', { name: /^Send / })).toBeDisabled();
});

/** The search page's send dialog, opened over the rows `harbour` answers. */
async function sendDialog(page: Page) {
	await page.goto('/datasets/lake~acme%24letters?project=letters');
	await page.getByRole('searchbox').fill('harbour');
	await page.keyboard.press('Enter');
	await page.getByRole('button', { name: 'Send to a project' }).click();
	return page.getByTestId('send-to-project');
}

test('a new choice of project starts with no label: a label is never carried into another project', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const dialog = await sendDialog(page);
	const label = dialog.getByLabel('Label every item as');
	await label.selectOption('letter');

	await dialog.getByTestId('send-project').selectOption('panel');
	await dialog.getByTestId('send-project').selectOption('letters');

	await expect(label).toHaveValue('');
	const posted = page.waitForRequest((req) => req.url().endsWith('/projects/letters/items'));
	await dialog.getByRole('button', { name: /^Send \d+ items$/ }).click();
	const items = ((await posted).postDataJSON() as { items: { prediction?: unknown }[] }).items;
	expect(items.every((item) => item.prediction === undefined)).toBe(true);
});

test('a project created around a selection whose send then fails is retried by sending alone, chosen', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	const dialog = await sendDialog(page);
	// The first send after the create gets no answer the app can use.
	await page.route('**/api/annotator/projects/*/items', (route) =>
		route.fulfill({
			status: 503,
			json: { detail: 'the annotator could not reach its state store' },
		}),
	);
	await dialog.getByLabel('Slug').fill('harbour-set');
	await dialog.getByRole('button', { name: /^Create and send/ }).click();
	await expect(dialog.getByTestId('send-error')).toHaveText(
		'the annotator could not reach its state store',
	);
	await page.unroute('**/api/annotator/projects/*/items');

	await expect(dialog.getByTestId('send-project')).toHaveValue(/^harbour-set-/);
	await dialog.getByRole('button', { name: /^Send \d+ items$/ }).click();

	await expect(dialog.getByTestId('send-done')).toBeVisible();
	const created = (await annotatorCalls(page, who)).filter(
		(c) => c.method === 'POST' && c.path === '/projects',
	);
	expect(created).toHaveLength(1);
});

test('a session that ended on the project page signs the person in again, never reading as a hidden project', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await page.goto('/projects');
	await expect(page.getByRole('link', { name: /Letters/ })).toBeVisible();
	// Signed in, the tokens in memory; the project's read is then refused as an ended session is.
	let refused = false;
	await page.route('**/api/annotator/projects/letters', (route) => {
		if (refused) return route.fallback();
		refused = true;
		return route.fulfill({ status: 401, json: { detail: 'the token expired' } });
	});
	const issuer = page.waitForRequest((req) => new URL(req.url()).pathname === '/oidc/auth');

	await page.getByRole('link', { name: /Letters/ }).click();
	await issuer;

	await expect(page.getByRole('heading', { level: 1, name: /Letters/ })).toBeVisible();
	await expect(page).toHaveURL(/\/projects\/letters/);
	await expect(page.getByText('that you can see')).toHaveCount(0);
});
