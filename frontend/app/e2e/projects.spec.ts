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
/** An item's name in the queue: its document's title and its page, the key only in its tooltip. */
const NAMES: Record<string, string> = {
	'letter-001/0': 'A letter from the harbour · page 1',
	'letter-001/1': 'A letter from the harbour · page 2',
	'letter-002/0': 'A letter about the harvest · page 1',
	'map-003/0': 'A map of the harbour · page 1',
};
const row = (page: Page, key: string) =>
	page.getByRole('row').filter({ has: page.locator(`[title^="${key} ·"]`) });

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

	// The next project starts from a blank task: the YAML view's task is not carried into it.
	await page.getByRole('button', { name: 'New project' }).click();
	await page.getByRole('button', { name: 'Task template' }).click();
	await page.getByRole('option', { name: 'custom (define in YAML)' }).click();
	await page.getByLabel('Slug').fill('scratch');
	await page.getByRole('button', { name: 'Create project' }).click();
	await expect(page.getByRole('link', { name: /scratch/ })).toBeVisible();
	await page.getByRole('button', { name: 'New project' }).click();
	await expect(page.getByRole('button', { name: 'Task template' })).toContainText(
		'custom (type your own classes)',
	);
	await page.getByLabel('Slug').fill('seals');
	await page.getByRole('textbox', { name: /Label classes/ }).fill('seal');
	const next = page.waitForRequest(
		(req) => req.method() === 'POST' && req.url().endsWith('/api/annotator/projects'),
	);
	await page.getByRole('button', { name: 'Create project' }).click();
	const seals = (await next).postDataJSON() as { ontology: { classes: { name: string }[] } };
	expect(seals.ontology.classes.map((c) => c.name)).toEqual(['seal']);
});

test('the queue offers the canvas’s events, claim, submit and fix & accept, and opens each item to annotate or to view', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	// While the project is still being read, the tab says only where the reader is, never the project's id: a screen
	// reader announces the title on arrival.
	let release!: () => void;
	const held = new Promise<void>((resolve) => (release = resolve));
	await page.route('**/api/annotator/projects/letters', async (route) => {
		await held;
		return route.fallback();
	});
	await letters(page);
	await expect(page).toHaveTitle('Projects · workbench');
	release();
	// The tab names the project by its title, as its breadcrumb does.
	await expect(page).toHaveTitle('Letters · Projects · workbench');
	await expect(row(page, 'letter-001/0').getByRole('button', { name: /^Claim/ })).toBeVisible();
	await expect(row(page, 'letter-001/1').getByRole('button', { name: /^Submit/ })).toBeVisible();
	await expect(
		row(page, 'letter-002/0').getByRole('button', { name: /^Fix & accept/ }),
	).toBeVisible();
	// The item the reader holds opens to annotate; one nobody holds opens to view.
	await expect(
		row(page, 'letter-001/1').getByRole('link', { name: `Annotate ${NAMES['letter-001/1']}` }),
	).toHaveAttribute('href', '/projects/letters/tasks/letters-t2?project=letters');
	await expect(
		row(page, 'letter-001/0').getByRole('link', { name: `View ${NAMES['letter-001/0']}` }),
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

test('a manager grants a role by name or by principal, and revoking the last owner is refused in the door’s words', async ({
	page,
	context,
}) => {
	await as(context, 'Owner');
	await letters(page);
	await page.getByTestId('tab-settings').click();
	const members = page.getByTestId('members-panel');
	await members.getByRole('button', { name: 'Show who has access' }).click();
	await expect(members.getByTestId('member-row')).toHaveCount(1);

	// Offered by name: the people of the projects the owner manages whom the annotator knows (they signed in).
	await expect(members.getByLabel('Person to grant')).toBeVisible();
	await members.getByLabel('Person to grant').click();
	await page.getByRole('option', { name: 'Alma Alpha' }).click();
	await members.getByTestId('grant-member').click();
	await expect(members.getByTestId('member-row').filter({ hasText: 'Alma Alpha' })).toBeVisible();
	// Anyone else by their principal: someone who has not signed in here yet is that principal, said so.
	await members.getByLabel('Principal to grant').fill('kc~fay');
	await members.getByTestId('grant-member').click();
	const fay = members.getByTestId('member-row').filter({ hasText: 'kc~fay' });
	await expect(fay).toContainText('has not signed in here yet');
	await expect(fay.getByRole('button', { name: 'Revoke annotator from fay' })).toBeVisible();
	await members.getByRole('button', { name: 'Revoke owner from Owner' }).click();

	await expect(members).toContainText(
		'Owner holds the only remaining owner on this project — grant another owner or manager first',
	);
	await expect(members.getByTestId('member-row')).toHaveCount(3);
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

test('a corpus of texts is browsed by its rows a page at a time, in the table too, and a page of them is sent as text', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await page.goto('/datasets/lake~acme%24memos?project=letters');

	await expect(page.getByText('Browsing 30 rows')).toBeVisible();
	const results = page.getByRole('list', { name: 'Search results' });
	await expect(results.getByRole('listitem')).toHaveCount(24);
	await page.getByRole('navigation', { name: 'Row pages' }).getByLabel('Next page').click();
	await expect(results.getByRole('listitem')).toHaveCount(6);
	await page.getByRole('button', { name: 'Table', exact: true }).click();
	await expect(
		page.getByRole('cell', { name: /Line 10: the harbour master/ }).first(),
	).toBeVisible();
	await page.getByRole('button', { name: 'Send to a project' }).click();
	const dialog = page.getByTestId('send-to-project');
	await expect(dialog).toContainText('From the rows listed on lake~acme$memos');
	const posted = page.waitForRequest((req) =>
		req.url().endsWith('/api/annotator/projects/letters/items'),
	);

	await dialog.getByRole('button', { name: 'Send 6 items', exact: true }).click();

	const items = (
		(await posted).postDataJSON() as {
			items: { source: { where: string; keys: string[] }; media: { kind: string } }[];
		}
	).items;
	expect(items.map((item) => [item.source.where, item.source.keys[0], item.media.kind])).toEqual(
		[4, 5, 6, 7, 8, 9].map((line) => ['lake~acme$memos', `memo2/${line}`, 'text']),
	);
	expect((await callsOf(page, who)).filter((call) => call.refused !== undefined)).toEqual([]);
	// Six items, each its own row's key, each called by its memo and its place in it, never alike.
	await dialog.getByRole('link', { name: 'Open the project' }).click();
	for (const line of [4, 9])
		await expect(row(page, `memo2/${line}`)).toContainText(`Memo 3 · part ${line + 1}`);
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

	// The chip, not the panel's instruction, which also says the word.
	await expect(adjudication.getByText('canonical', { exact: true })).toBeVisible();
	await expect(adjudication.getByRole('button', { name: 'Pick', exact: true })).toHaveCount(0);
	await expect(adjudication.getByRole('button', { name: 'Re-pick' })).toHaveCount(1);
	await page.getByTestId('tab-items').click();
	const replica = (key: string, by: string) => row(page, key).filter({ hasText: by });
	await expect(replica('letter-001/0', 'Alma Alpha').getByText('canonical')).toBeVisible();
	await expect(replica('letter-001/0', 'Bert Beta').getByText('canonical')).toHaveCount(0);
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
	await page.getByRole('dialog').getByRole('textbox', { name: 'Their principal' }).fill('kc~fay');
	await page.getByRole('dialog').getByRole('button', { name: 'Assign', exact: true }).click();

	await expect(row(page, 'letter-001/0')).toContainText('claimed');
	// fay has written nothing, so no name is kept for her: her principal's subject, the principal its tooltip.
	await expect(row(page, 'letter-001/0').getByTitle(/kc~fay/)).toContainText('fay · pinned');
});

test('a bulk accept fires one gated event per item, and a partial failure names what failed and what landed', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Manager');
	await page.request.post(
		`${EDGE}/api/viewer/__fault?sub=${encodeURIComponent(who.sub)}&path=/tasks/judging-b/events&kind=fail`,
	);
	await page.goto('/projects/judging?state=in_review');
	await page.getByRole('checkbox', { name: 'Select all rows' }).check();

	await page.getByTestId('bulk-accept').click();

	await expect(
		page.getByText(
			/^2 of 3 — A letter about the harvest · page 1: the annotator could not reach its state store$/,
		),
	).toBeVisible();
	await expect(page.getByTestId('state-accepted')).toHaveText(/accepted\s*3$/);
	await expect(page.getByTestId('state-in_review')).toHaveText(/in review\s*1$/);
});

test('an item the reader submitted is offered no verdict in the table, the quick view or a bulk accept', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await page.setViewportSize({ width: 1440, height: 900 });
	await panel(page);
	const replicas = row(page, 'letter-002/0');
	const own = replicas.filter({ hasNotText: 'Alma Alpha' });
	const verdict = { name: /^(Accept|Fix & accept|Request changes…)$/ };
	await expect(replicas.filter({ hasText: 'Alma Alpha' }).getByRole('button', verdict)).toHaveCount(
		3,
	);
	await expect(own.getByRole('button', verdict)).toHaveCount(0);
	await own.getByRole('button', { name: /beside the table/ }).click();
	const quick = page.getByTestId('quick-view');
	// The pane names the item as the table does, and its actions stay in view at a laptop's height.
	await expect(quick.getByTestId('quick-name')).toHaveText('A letter about the harvest · page 1');
	await expect(quick.getByTestId('quick-accept')).toHaveCount(0);
	await expect(quick.getByTestId('quick-next')).toBeInViewport();
	await replicas.nth(0).getByRole('checkbox').check();
	await replicas.nth(1).getByRole('checkbox').check();

	await page.getByTestId('bulk-accept').click();

	await expect(page.getByTestId('queue-notice')).toHaveText('Accept: 1 item.');
	await expect(replicas.filter({ hasText: 'Alma Alpha' })).toContainText('accepted');
	await expect(own).toContainText('in review');
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

test('a project created around a selection is made with its classes, and one whose send then fails is retried by sending alone, chosen', async ({
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
	// A project with no classes would leave every item it is sent unclassable: the dialog asks for them first.
	const create = dialog.getByRole('button', { name: /^Create and send/ });
	await expect(create).toBeDisabled();
	await dialog.getByTestId('send-new-classes').fill('letter, map, letter');
	await dialog.getByLabel('Shape kind', { exact: true }).click();
	await page.getByRole('option', { name: 'polygon' }).click();
	const createdBody = page.waitForRequest(
		(req) => req.method() === 'POST' && new URL(req.url()).pathname === '/api/annotator/projects',
	);
	await create.click();
	expect(
		((await createdBody).postDataJSON() as { ontology: { classes: unknown[] } }).ontology.classes,
	).toEqual([
		{ name: 'letter', tools: ['polygon'], required: false },
		{ name: 'map', tools: ['polygon'], required: false },
	]);
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

test('the data manager filters, counts and sorts in the annotator, and its address restores the view', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await page.goto('/projects/judging');
	await expect(page.getByTestId('state-in_review')).toContainText('3');
	await expect(page.getByTestId('state-accepted')).toContainText('1');

	const filtered = page.waitForRequest((req) =>
		req.url().includes('/projects/judging/tasks?state=in_review'),
	);
	await page.getByTestId('state-in_review').click();
	await filtered;
	await page.getByRole('button', { name: 'item', exact: true }).click();
	await expect(page).toHaveURL(/[?&]state=in_review.*&order=desc/);
	await page.reload();

	await expect(page.getByTestId('state-in_review')).toHaveAttribute('aria-pressed', 'true');
	await expect(page.getByTestId('filter-count')).toContainText('3 of 5 items match.');
	const rows = page
		.locator('[data-slot="data-table-card"]')
		.getByRole('row')
		.filter({ has: page.getByTestId('person') });
	await expect(rows).toHaveCount(3);
	// The newest item first, and each submitter by the name they last wrote under, their principal the tooltip.
	await expect(rows.first()).toContainText('Sam Else');
	await expect(rows.first().getByTitle('user:someone-else')).toHaveText('Sam Else');
	await expect(rows.nth(1)).toContainText('Bert Beta');
	await expect(rows.nth(2)).toContainText('Alma Alpha');
});

test('select all matching reads the filter a page at a time up to its cap and says so, and a date range reads the days it names', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	const offsets: string[] = [];
	page.on('request', (req) => {
		const url = new URL(req.url());
		if (url.pathname.endsWith('/projects/heap/tasks') && url.searchParams.get('limit') === '200')
			offsets.push(url.searchParams.get('offset') ?? '');
	});
	await page.goto('/projects/heap');
	await page.getByRole('checkbox', { name: 'Select all rows' }).check();

	await page.getByTestId('select-all-matching').click();

	await expect(page.getByTestId('select-all-held')).toContainText(
		'1,000 matching items are selected.',
	);
	await expect(page.getByTestId('queue-notice')).toHaveText(
		'The filter matches 1,050 items: the first 1,000 are selected. Narrow the filter to act on the rest.',
	);
	expect(offsets).toEqual(['0', '200', '400', '600', '800']);
	await page.getByTestId('select-all-clear').click();
	await page.getByLabel('Which date the range reads').click();
	await expect(page.getByRole('option', { name: 'Sent' })).toBeVisible();
	// The field and both days chosen in one go, each before the address holds the one before: none is lost.
	await page.evaluate(() => {
		const sent = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(
			(o) => o.textContent?.trim() === 'Sent',
		);
		// The listbox selects on pointerup (bits-ui's select item).
		sent?.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerType: 'mouse' }));
		for (const id of ['filter-since', 'filter-until']) {
			const day = document.querySelector<HTMLInputElement>(`[data-testid="${id}"]`);
			if (!day) throw new Error(`no ${id}`);
			day.value = '2026-11-01';
			day.dispatchEvent(new Event('change', { bubbles: true }));
		}
	});
	await expect(page).toHaveURL(/date=sent&since=2026-11-01&until=2026-11-01/);
	await expect(page.getByTestId('filter-count')).toHaveText(/^50 of 1,050\s+items match\./);
});

test('a bulk assign over the items a filter matches assigns each one to the person named', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await page.goto('/projects/letters?state=unassigned');
	const unassigned = page.getByRole('row').filter({ hasText: 'unassigned' });
	await expect(unassigned.first()).toBeVisible();
	const count = await unassigned.count();
	expect(count).toBeGreaterThan(0);
	await page.getByRole('checkbox', { name: 'Select all rows' }).check();

	await page.getByTestId('bulk-assign').click();
	await page.getByRole('dialog').getByRole('textbox', { name: 'Their principal' }).fill('kc~fay');
	await page.getByRole('dialog').getByRole('button', { name: 'Assign', exact: true }).click();

	await expect(page.getByTestId('queue-notice')).toHaveText(
		`Assigned to fay: ${count} item${count === 1 ? '' : 's'}.`,
	);
	await expect(page.getByTestId('state-unassigned')).toContainText('0');
	await page.getByTestId('clear-filter').click();
	await expect(page.getByText('fay · pinned')).toHaveCount(count);
});

test('the items tab is read again when the reader comes back to the window, and by Refresh', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await page.goto('/projects/letters');
	const claimed = page.getByTestId('state-claimed');
	const reads = (n: number) => new RegExp(`claimed\\s*${n}$`);
	await expect(claimed).toHaveText(reads(1));
	// The same person acts in another tab (the mock keeps one world per person): someone acted meanwhile.
	const other = await context.newPage();
	await other.goto('/projects/letters');
	const act = async (event: 'Claim' | 'Release', left: number): Promise<void> => {
		await other.getByRole('button', { name: event, exact: true }).first().click();
		await expect(other.getByTestId('state-claimed')).toHaveText(reads(left));
	};
	await act('Claim', 2);
	await expect(claimed).toHaveText(reads(1));

	await page.bringToFront();
	await page.evaluate(() => window.dispatchEvent(new Event('focus')));

	await expect(claimed).toHaveText(reads(2));
	await act('Release', 1);
	const listed = page.waitForRequest(
		(req) =>
			req.url().includes('/projects/letters/tasks?') && req.url().includes('include=details'),
	);
	await page.getByRole('button', { name: 'Refresh' }).click();
	await listed;
	await expect(claimed).toHaveText(reads(1));
});

test('a manager changes the settings, and what may no longer change says why', async ({
	page,
	context,
}) => {
	await as(context, 'Manager');
	await page.goto('/projects/letters?tab=settings');
	await expect(page.getByTestId('settings-consensus')).toBeDisabled();
	await expect(
		page.getByText('Fixed: items were sent, each made into this many replicas.'),
	).toBeVisible();
	await page.getByTestId('settings-title').fill('Harbour letters');
	await page
		.getByTestId('settings-instructions')
		.fill('Tag a page that is a letter, and only those.');

	const patched = page.waitForRequest(
		(req) => req.method() === 'PATCH' && req.url().endsWith('/api/annotator/projects/letters'),
	);
	await page.getByTestId('settings-save').click();

	expect((await patched).postDataJSON()).toEqual({
		title: 'Harbour letters',
		instructions: 'Tag a page that is a letter, and only those.',
	});
	await expect(page.getByRole('heading', { level: 1, name: /Harbour letters/ })).toBeVisible();
	await expect(page.getByTestId('instructions')).toContainText('and only those');
	await expect(page).toHaveURL(/tab=settings/);
});

test('the review tab lists the work waiting for the reviewer by its annotators’ names, and none of their own', async ({
	page,
	context,
}) => {
	await as(context, 'Reviewer');
	await page.goto('/projects/judging?tab=review');

	await expect(page.getByTestId('review-waiting')).toHaveText('3 items wait for your review.');
	const list = page.getByTestId('review-list');
	await expect(list.getByTestId('person')).toHaveText(['Alma Alpha', 'Bert Beta', 'Sam Else']);
	await expect(page.getByTestId('review-start')).toHaveAttribute(
		'href',
		/\/projects\/judging\/tasks\/judging-a\?of=3&pass=review&project=judging$/,
	);
});

// THE BULK-LABELING GRID (`?tab=label`): the `sorting` project's items open to the reader, labelled from the keyboard.
const grid = (page: Page) => page.getByTestId('label-grid');
const cellOf = (page: Page, task: string) =>
	page.locator(`[data-testid="grid-cell"][data-task="${task}"]`);
const labelsOf = (page: Page, task: string) => cellOf(page, task).getByTestId('grid-cell-labels');

test('the grid labels items by keyboard alone, each claimed and saved on its own, and a reload comes back to the item in focus', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');

	// The reader's own claim and the 24 nobody holds; neither another's claim nor work in review.
	await expect(grid(page).getByRole('gridcell')).toHaveCount(25);
	await expect(cellOf(page, 'sorting-theirs')).toHaveCount(0);
	await expect(
		page.getByTestId('grid-palette').getByRole('button', { name: /letter/ }),
	).toHaveAttribute('aria-keyshortcuts', '1');
	await expect(page.getByTestId('grid-canvas-classes')).toHaveText(
		'Labelled on the canvas, one item at a time: stamp (bbox).',
	);

	await cellOf(page, 'sorting-00').focus();
	// A digit labels the item in focus and moves on: letter, then map.
	await page.keyboard.press('1');
	await page.keyboard.press('2');
	await expect(cellOf(page, 'sorting-02')).toBeFocused();
	// Shift with an arrow selects from the item in focus; a digit labels the selection.
	for (let i = 0; i < 3; i++) await page.keyboard.press('Shift+ArrowRight');
	await expect(grid(page).locator('[aria-selected="true"]')).toHaveCount(4);
	await page.keyboard.press('1');

	await expect(labelsOf(page, 'sorting-00')).toHaveText('letter');
	await expect(labelsOf(page, 'sorting-01')).toHaveText('map');
	for (const id of ['sorting-02', 'sorting-03', 'sorting-04', 'sorting-05'])
		await expect(labelsOf(page, id)).toHaveText('letter');
	await expect(page.getByTestId('grid-notice')).toHaveText('Labelled letter: 4 changed.');
	await expect(cellOf(page, 'sorting-05')).toHaveAttribute('aria-label', /labelled letter/);
	// One claim and one save per item, each through its own door.
	const calls = await annotatorCalls(page, who);
	const claims = calls.filter(
		(c) => c.method === 'POST' && /^\/tasks\/sorting-\d+\/events$/.test(c.path),
	);
	const saves = calls.filter((c) => c.method === 'POST' && c.path.startsWith('/api/annotations/'));
	expect([
		claims.length,
		saves.length,
		[...claims, ...saves].every((c) => c.status === 200),
	]).toEqual([6, 6, true]);

	await expect(page).toHaveURL(/[?&]at=sorting-05/);
	await page.reload();
	await expect(cellOf(page, 'sorting-05')).toHaveAttribute('tabindex', '0');
	await expect(labelsOf(page, 'sorting-05')).toHaveText('letter');
});

test('the grid undoes and redoes its last label, and clears one, each on the revision the item was left at', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Shift+ArrowRight');
	await page.keyboard.press('1');
	await expect(labelsOf(page, 'sorting-01')).toHaveText('letter');
	// Alt with a digit adds a label beside the one held.
	await page.keyboard.press('Alt+2');
	await expect(labelsOf(page, 'sorting-01')).toHaveText(/letter\s*map/);

	await page.keyboard.press('Control+z');
	await expect(labelsOf(page, 'sorting-01')).toHaveText('letter');
	await expect(page.getByTestId('grid-notice')).toHaveText('Undone: Toggled map: 2 changed.');
	await page.keyboard.press('Control+Shift+z');
	await expect(labelsOf(page, 'sorting-00')).toHaveText(/letter\s*map/);

	await page.keyboard.press('Escape');
	await expect(grid(page).locator('[aria-selected="true"]')).toHaveCount(0);
	await page.keyboard.press('Delete');
	await expect(labelsOf(page, 'sorting-01')).toHaveText('no label');
	await expect(labelsOf(page, 'sorting-00')).toHaveText(/letter\s*map/);
});

test('a label over a selection one item of which was released meanwhile labels the rest and names the one refused', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	await expect(cellOf(page, 'sorting-mine')).toBeVisible();
	await cellOf(page, 'sorting-22').click();
	await cellOf(page, 'sorting-mine').click({ modifiers: ['Shift'] });
	await expect(grid(page).locator('[aria-selected="true"]')).toHaveCount(3);
	// The same person releases their claim in another tab (the mock keeps one world per person).
	const other = await context.newPage();
	await other.goto('/projects/sorting?state=claimed');
	await other.getByRole('button', { name: 'Release', exact: true }).click();
	await expect(other.getByTestId('state-claimed')).toHaveText(/claimed\s*1$/);

	await page.getByTestId('grid-label-map').click();

	await expect(page.getByTestId('grid-notice')).toContainText(
		'Labelled map: 2 changed, 1 refused.',
	);
	await expect(page.getByTestId('grid-refusals')).toHaveText(
		'this item is unassigned — its shapes take edits only while it is claimed or in review (1): A map of the harbour · page 1',
	);
	await expect(labelsOf(page, 'sorting-22')).toHaveText('map');
	await expect(labelsOf(page, 'sorting-23')).toHaveText('map');
	await expect(labelsOf(page, 'sorting-mine')).toHaveText('no label');
	await expect(cellOf(page, 'sorting-mine').getByTestId('grid-cell-refused')).toBeVisible();
});

test('the labelled items are submitted together, each answered, and the items tab counts them', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Shift+ArrowRight');
	await page.keyboard.press('Shift+ArrowRight');
	await page.keyboard.press('2');
	await expect(labelsOf(page, 'sorting-02')).toHaveText('map');
	await page.keyboard.press('Control+a');

	// Submit is offered for the items the reader holds: the three just labelled (and claimed) and their own claim.
	await page.getByTestId('grid-bulk-submit').click();

	await expect(page.getByTestId('grid-notice')).toHaveText('Submit for review: 4 done.');
	await expect(grid(page).getByRole('gridcell')).toHaveCount(21);
	await page.getByTestId('tab-items').click();
	await expect(page.getByTestId('state-in_review')).toHaveText(/in review\s*6$/);
});

test('a reviewer accepts many and sends some back with one note, from the grid', async ({
	page,
	context,
}) => {
	await as(context, 'Reviewer');
	await page.goto('/projects/sorting?tab=label&pass=review');
	await expect(grid(page).getByRole('gridcell')).toHaveCount(2);
	await expect(labelsOf(page, 'sorting-review-b')).toHaveText('map');

	await cellOf(page, 'sorting-review-a').click();
	await page.getByTestId('grid-bulk-request_changes').click();
	await page.getByRole('textbox', { name: 'What should change' }).fill('Which page is this?');
	await page.getByTestId('grid-send-back').getByRole('button', { name: 'Send back' }).click();
	await expect(grid(page).getByRole('gridcell')).toHaveCount(1);

	await cellOf(page, 'sorting-review-b').click();
	await page.getByTestId('grid-bulk-accept').click();
	await expect(page.getByTestId('grid-empty')).toHaveText('Nothing waits for your review.');
	await page.getByTestId('tab-items').click();
	await expect(page.getByTestId('state-accepted')).toHaveText(/accepted\s*1$/);
	await expect(page.getByTestId('state-changes_requested')).toHaveText(/changes requested\s*1$/);
});

test('a label that would replace what another label carries is asked about once, skipped by default, and an undo brings it back whole', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	await expect(cellOf(page, 'sorting-20').getByTestId('grid-cell-details')).toBeVisible();
	await expect(cellOf(page, 'sorting-21').getByTestId('grid-cell-suggested')).toHaveText('map?');
	await cellOf(page, 'sorting-19').focus();
	await page.keyboard.press('Shift+ArrowRight');
	await page.keyboard.press('Shift+ArrowRight');

	await page.keyboard.press('2');
	// Only the item whose `letter` carries words is asked about: setting `map` where a model proposed it confirms it.
	await expect(page.getByTestId('grid-ask-count')).toContainText('1 of 3 items carry');
	// Skipping is the default: the question opens on it, and Enter takes it.
	await expect(page.getByTestId('grid-ask-skip')).toBeFocused();
	await page.keyboard.press('Enter');

	await expect(page.getByTestId('grid-notice')).toContainText(
		'Labelled map: 2 changed, 1 not attempted.',
	);
	await expect(labelsOf(page, 'sorting-20')).toHaveText('letter');
	await expect(labelsOf(page, 'sorting-21')).toHaveText('map');
	await expect(cellOf(page, 'sorting-21').getByTestId('grid-cell-suggested')).toHaveCount(0);

	await cellOf(page, 'sorting-20').click();
	await page.keyboard.press('2');
	await page.getByTestId('grid-ask-replace').click();
	await expect(labelsOf(page, 'sorting-20')).toHaveText('map');
	await expect(cellOf(page, 'sorting-20').getByTestId('grid-cell-details')).toHaveCount(0);
	// A key belongs to the question while it is on the page (`walk-keys.ts`), so the undo waits for it to go.
	await expect(page.getByTestId('grid-ask')).toHaveCount(0);
	await page.keyboard.press('Control+z');
	await expect(labelsOf(page, 'sorting-20')).toHaveText('letter');
	await expect(cellOf(page, 'sorting-20').getByTestId('grid-cell-details')).toBeVisible();
});

test('a long run says how far it is and stops when asked, the items in flight finished and the rest untouched', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	for (const id of ['sorting-00', 'sorting-01', 'sorting-02', 'sorting-03'])
		await page.request.post(
			`${EDGE}/api/viewer/__fault?sub=${encodeURIComponent(who.sub)}&path=/tasks/${id}/events&kind=slow`,
		);
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Control+a');

	await page.keyboard.press('1');
	// The model's `map` proposal on one item is asked about first; it is kept.
	await expect(page.getByTestId('grid-ask-count')).toContainText('1 of 25 items carry');
	await expect(page.getByTestId('grid-ask-skip')).toBeFocused();
	await page.keyboard.press('Enter');
	await expect(page.getByTestId('grid-progress')).toContainText('Labelled letter: 0 of 25');
	await page.getByTestId('grid-stop').click();

	await expect(page.getByTestId('grid-notice')).toContainText(
		'Labelled letter: 4 changed, 21 not attempted.',
		{ timeout: 10_000 },
	);
	await expect(page.getByTestId('grid-skipped')).toContainText(
		'Not attempted, stopped before it was started (21)',
	);
	await expect(labelsOf(page, 'sorting-03')).toHaveText('letter');
	await expect(labelsOf(page, 'sorting-04')).toHaveText('no label');
	await expect(cellOf(page, 'sorting-04').getByText('unassigned', { exact: true })).toBeVisible();
});

test('an item the grid claimed only to label is released again when its label is refused', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await page.request.post(
		`${EDGE}/api/viewer/__fault?sub=${encodeURIComponent(who.sub)}&path=/api/annotations/letter-002/0&kind=fail`,
	);
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-01').focus();
	await page.keyboard.press('Shift+ArrowRight');

	await page.keyboard.press('1');

	await expect(page.getByTestId('grid-refusals')).toHaveText(
		'not labelled, and released again: the annotator could not read the task’s work (1): A letter about the harvest · page 1',
	);
	await expect(labelsOf(page, 'sorting-01')).toHaveText('letter');
	await expect(cellOf(page, 'sorting-02').getByText('unassigned', { exact: true })).toBeVisible();
	await expect(cellOf(page, 'sorting-01').getByText('claimed', { exact: true })).toBeVisible();
});

test('a label or an undo whose answer was lost is judged by what the item now holds, not refused', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	// The write reaches the annotator and lands; its answer never reaches the page.
	const lose = (path: RegExp) =>
		page.route(path, async (route) => {
			if (route.request().method() !== 'POST') return route.fallback();
			await route.fetch();
			return route.abort('connectionreset');
		});
	await lose(/\/api\/annotations\/[^?]+\/0\?/);
	await cellOf(page, 'sorting-00').focus();

	await page.keyboard.press('1');

	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Labelled letter: 0 changed, 1 already so.',
	);
	await expect(labelsOf(page, 'sorting-00')).toHaveText('letter');
	await page.unrouteAll({ behavior: 'wait' });
	await cellOf(page, 'sorting-01').focus();
	await page.keyboard.press('1');
	await expect(labelsOf(page, 'sorting-01')).toHaveText('letter');
	await lose(/\/revert\?/);

	await page.keyboard.press('Control+z');

	await expect(page.getByTestId('grid-notice')).toHaveText('Undone: Labelled letter: 1 changed.');
	await expect(labelsOf(page, 'sorting-01')).toHaveText('no label');
});
