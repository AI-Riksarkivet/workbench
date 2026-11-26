import {
	expect,
	test,
	type BrowserContext,
	type Locator,
	type Page,
	type Request,
	type Route,
} from '@playwright/test';
import { callsOf, person, signIn, watchCsp, type Call } from './session';
import { EDGE } from './ports';
import type { MockIdentity } from './mock-oidc';
import { SENT_BACK_NOTE, type Elsewhere, type Fault, type WorkElsewhere } from './mock-projects';
import { FIRST_KEY } from './dpop';

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
	// An action on one item says what was done, naming it.
	await row(page, 'letter-001/1')
		.getByRole('button', { name: /^Submit/ })
		.click();
	await expect(page.getByTestId('queue-notice')).toHaveText(
		`Submitted for review: ${NAMES['letter-001/1']}.`,
	);
});

test('an annotator is offered no review of others’ work', async ({ page, context }) => {
	await as(context, 'Annotator');
	await letters(page);

	await expect(page.getByTestId('reader-role')).toHaveText(
		'You annotate in this project: you claim items and label them.',
	);
	await expect(
		row(page, 'letter-002/0').getByRole('button', { name: /Accept|Send back/ }),
	).toHaveCount(0);
});

test('a reviewer requests changes with a note, and the note is read on the item’s row by whom, whole on demand', async ({
	page,
	context,
}) => {
	await as(context, 'Reviewer');
	await letters(page);

	const form = page.getByTestId('items-send-back');
	await row(page, 'letter-002/0').getByRole('button', { name: 'Send back' }).click();
	await form.getByRole('textbox').fill('A first thought.');
	// A typed note is not lost by a click outside or by Cancel: the form asks; discarding empties it.
	await page.mouse.click(5, 5);
	await expect(form.getByTestId('send-back-discard')).toBeVisible();
	await form.getByRole('button', { name: 'Keep writing' }).click();
	await form.getByRole('button', { name: 'Cancel' }).click();
	await form.getByRole('button', { name: 'Discard' }).click();
	await expect(form).toHaveCount(0);
	await row(page, 'letter-002/0').getByRole('button', { name: 'Send back' }).click();
	await expect(form.getByRole('textbox')).toHaveValue('');
	await form.getByRole('textbox').fill(SENT_BACK_NOTE);
	await form.getByRole('button', { name: 'Send back' }).click();

	await expect(row(page, 'letter-002/0')).toContainText('changes requested');
	const note = row(page, 'letter-002/0').getByTestId('row-review-note');
	// By the reviewer's name, as they signed in.
	await expect(note.locator('summary')).toContainText('Reviewer: Check the class');
	await note.locator('summary').click();
	await expect(note.getByRole('listitem')).toContainText(SENT_BACK_NOTE);
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
	// Assigned from its own row, the item is named.
	await expect(page.getByTestId('queue-notice')).toHaveText(
		`Assigned to fay: ${NAMES['letter-001/0']}.`,
	);
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
	const verdict = { name: /^(Accept|Fix & accept|Send back…)$/ };
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

	await expect(page.getByTestId('queue-notice')).toHaveText('Accepted: 1 item.');
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
		return route.fulfill({
			status: 401,
			json: { detail: 'the token expired' },
			headers: { 'www-authenticate': 'DPoP error="invalid_token"' },
		});
	});
	await page.getByRole('link', { name: /Letters/ }).click();
	await expect(page.getByTestId('project-signin')).toBeVisible();
	const issuer = page.waitForRequest((req) => new URL(req.url()).pathname === '/oidc/auth');
	await page.getByTestId('session-prompt').getByRole('button', { name: 'Sign in again' }).click();
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
	const act = async (event: 'Claim' | 'Release', left: number, done: string): Promise<void> => {
		await other.getByRole('button', { name: event, exact: true }).first().click();
		await expect(other.getByTestId('state-claimed')).toHaveText(reads(left));
		// Said in the past, naming the item.
		await expect(other.getByTestId('queue-notice')).toHaveText(
			new RegExp(`^${done}: A [^:]+ · page \\d\\.$`),
		);
	};
	await act('Claim', 2, 'Claimed');
	await expect(claimed).toHaveText(reads(1));

	await page.bringToFront();
	await page.evaluate(() => window.dispatchEvent(new Event('focus')));

	await expect(claimed).toHaveText(reads(2));
	await act('Release', 1, 'Released');
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

// THE JUDGE (ADR 0026): `vetting` names the stand-in judge, which doubts every caption (suggesting a figure) and an item
// nobody labelled; `appeals` names the stand-in that fails every ask (`e2e/mock-projects.ts`).
const judgeAsks = async (page: Page, who: MockIdentity): Promise<Call[]> =>
	(await annotatorCalls(page, who)).filter(
		(c) => c.method === 'POST' && c.path.startsWith('/api/judge/'),
	);
const DOUBT = "the stand-in judge doubts every 'caption': it would say 'figure'";
/** An item's name in the lists, by its key; a key with no name is a mistake in the spec. */
const nameOf = (key: string): string => {
	const name = NAMES[key];
	if (name === undefined) throw new Error(`no item is named for ${key}`);
	return name;
};

test('the review tab asks the judge about the items it has not read, then lists the flagged first with what it disagreed with and the model that judged', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Reviewer');
	await page.goto('/projects/vetting?tab=review');

	await expect(page.getByTestId('review-judge')).toHaveText(
		'Judge stand-in-judge: 3 flagged, 1 clear.',
	);
	// The three items no judge had read were asked about once each; the one judged at its submit was not.
	expect((await judgeAsks(page, who)).map((c) => [c.path, c.status]).sort()).toEqual([
		['/api/judge/letter-001/0', 200],
		['/api/judge/letter-001/1', 200],
		['/api/judge/letter-002/0', 200],
	]);
	// Flagged first, each the longest waiting first, then the clear one.
	const items = page.getByTestId('review-list').locator(':scope > li');
	await expect(items).toHaveCount(4);
	await expect(items.nth(0)).toContainText(nameOf('letter-001/1'));
	await expect(items.nth(0).getByTestId('judge-flagged')).toContainText('Flagged');
	await expect(items.nth(0).getByTestId('judge-flag')).toHaveText(
		`A label: ${DOUBT} (suggests figure)`,
	);
	await expect(items.nth(0).getByTestId('judge-by')).toHaveText(
		'judged by stand-in-judge stand-in.1',
	);
	await expect(items.nth(1)).toContainText(nameOf('letter-002/0'));
	await expect(items.nth(1).getByTestId('judge-flag')).toHaveText(
		'Whole item: nothing is labelled on this item',
	);
	await expect(items.nth(2)).toContainText(nameOf('map-003/0'));
	await expect(items.nth(2).getByTestId('judge-flagged')).toBeVisible();
	await expect(items.nth(3)).toContainText(nameOf('letter-001/0'));
	await expect(items.nth(3).getByTestId('judge-flagged')).toHaveCount(0);
	await expect(items.nth(3).getByTestId('judge-clear')).toHaveText(
		'Nothing flagged, judged by stand-in-judge stand-in.1',
	);
	// The pass starts at the first flagged item.
	await expect(page.getByTestId('review-start')).toHaveAttribute(
		'href',
		/\/projects\/vetting\/tasks\/vetting-b\?/,
	);
});

test('a project with no judge lists its review as it does without one, and says quietly that it has none', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Reviewer');
	await page.goto('/projects/judging?tab=review');

	await expect(page.getByTestId('review-judge')).toHaveText('This project has no judge.');
	await expect(page.getByTestId('review-list').getByTestId('person')).toHaveText([
		'Alma Alpha',
		'Bert Beta',
		'Sam Else',
	]);
	await expect(page.getByTestId('review-list').getByTestId(/^judge-/)).toHaveCount(0);
	expect(await judgeAsks(page, who)).toEqual([]);
});

test('a judge that fails is said unavailable, is asked once per item and no more, and the list still works', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Reviewer');
	await page.goto('/projects/appeals?tab=review');

	await expect(page.getByTestId('review-judge')).toHaveText(
		'The judge is unavailable: the model endpoint failed (500): stand-in-failing fails every ask',
	);
	const items = page.getByTestId('review-list').locator(':scope > li');
	await expect(items).toHaveCount(4);
	await expect(items.getByTestId('judge-unread')).toHaveCount(4);
	await expect(page.getByTestId('review-start')).toBeVisible();
	// The four unread items went out at once, each failing alike.
	expect((await judgeAsks(page, who)).map((c) => c.status)).toEqual([502, 502, 502, 502]);
	// A judge that failed is asked no more: the list read again by Refresh asks nothing.
	const reread = page.waitForResponse(
		(res) => res.url().includes('/projects/appeals/tasks?') && res.request().method() === 'GET',
	);
	await page.getByRole('button', { name: 'Refresh' }).click();
	await reread;
	await expect(items.getByTestId('judge-unread')).toHaveCount(4);
	expect(await judgeAsks(page, who)).toHaveLength(4);

	// A deployment that names no model endpoint: the listing says why, and nothing is asked.
	await faultOn(page, who, '/api/assist/models', 'no-endpoint');
	await page.reload();
	await expect(page.getByTestId('review-judge')).toHaveText(
		'The judge is unavailable: assist is unavailable: this deployment names no model endpoint',
	);
	await expect(items).toHaveCount(4);
	expect(await judgeAsks(page, who)).toHaveLength(4);
});

test('an item refused for itself stays not judged and is asked no more, while the rest are judged and the judge is not called unavailable', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Reviewer');
	// The first item leaves review while its judge is asked.
	await faultOn(page, who, '/api/judge/letter-001/0', 'left-review');
	await page.goto('/projects/vetting?tab=review');

	await expect(page.getByTestId('review-judge')).toHaveText(
		'Judge stand-in-judge: 3 flagged, 0 clear, 1 not judged.',
	);
	const items = page.getByTestId('review-list').locator(':scope > li');
	await expect(items.nth(3)).toContainText(nameOf('letter-001/0'));
	await expect(items.nth(3).getByTestId('judge-unread')).toHaveText('Not judged yet');
	// Asked once each: the list read again after the others were judged does not ask the refused one again.
	expect((await judgeAsks(page, who)).map((c) => [c.path, c.status]).sort()).toEqual([
		['/api/judge/letter-001/0', 409],
		['/api/judge/letter-001/1', 200],
		['/api/judge/letter-002/0', 200],
	]);
});

test('a submit that lands in review asks the project’s judge about the item, and a project with none is not asked', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	// A project with no judge first: its submit asks nothing.
	await page.goto('/projects/letters?state=claimed');
	await row(page, 'letter-001/1')
		.getByRole('button', { name: /^Submit/ })
		.click();
	await expect(row(page, 'letter-001/1')).toHaveCount(0);

	await page.goto('/projects/vetting?state=claimed');
	await row(page, 'letter-001/0')
		.getByRole('button', { name: /^Submit/ })
		.click();
	await expect(row(page, 'letter-001/0')).toHaveCount(0);
	// The one ask is the judging project's: none came of the first submit, a page and a submit earlier.
	await expect
		.poll(async () => (await judgeAsks(page, who)).map((c) => [c.path, c.status]))
		.toEqual([['/api/judge/letter-001/0', 200]]);
});

test('sending a flagged item back from the items table or the grid asks whether the judge was right, and only an untick says no, for the flagged items alone', async ({
	page,
	context,
}) => {
	await as(context, 'Reviewer');
	const fired: Record<string, unknown> = {};
	page.on('request', (req) => {
		const at = /\/tasks\/([\w-]+)\/events$/.exec(new URL(req.url()).pathname);
		if (at?.[1] && req.method() === 'POST') fired[at[1]] = req.postDataJSON();
	});
	// The items table: `vetting-d` was flagged at its submit.
	await page.goto('/projects/vetting');
	await row(page, 'map-003/0').getByRole('button', { name: 'Send back…', exact: true }).click();
	const form = page.getByTestId('items-send-back');
	await expect(form.getByRole('heading')).toHaveText(`Send back ${NAMES['map-003/0']}`);
	const question = form.getByTestId('send-back-judge');
	await expect(question).toHaveText("The judge's flag was right");
	await expect(question.getByRole('checkbox')).toBeChecked();
	await question.getByRole('checkbox').click();
	await form.getByRole('textbox').fill('The caption is a figure after all? No.');
	await form.getByRole('button', { name: 'Send back' }).click();
	await expect(row(page, 'map-003/0')).toContainText('changes requested');
	expect(fired['vetting-d']).toEqual({
		event: 'request_changes',
		message: 'The caption is a figure after all? No.',
		judge_agreed: false,
	});

	// The grid: once the Review tab has had the rest judged, one note over an item found clear and one flagged, the
	// question left ticked: nothing is said of the flag.
	await page.goto('/projects/vetting?tab=review');
	await expect(page.getByTestId('review-judge')).toHaveText(
		'Judge stand-in-judge: 2 flagged, 1 clear.',
	);
	await page.goto('/projects/vetting?tab=label&pass=review');
	await cellOf(page, 'vetting-a').click();
	await page.keyboard.press('Shift+ArrowRight');
	await expect(page.getByTestId('grid-bulk-request_changes')).toHaveText('Send back 2');
	await page.getByTestId('grid-bulk-request_changes').click();
	const gridForm = page.getByTestId('grid-send-back');
	await expect(gridForm.getByRole('heading')).toHaveText('Send back 2 items');
	await expect(gridForm.getByTestId('send-back-judge')).toHaveText("The judge's flag was right");
	await gridForm.getByRole('textbox').fill('Look again.');
	await gridForm.getByRole('button', { name: 'Send back' }).click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Sent back: 2 items.');
	expect(fired['vetting-a']).toEqual({ event: 'request_changes', message: 'Look again.' });
	expect(fired['vetting-b']).toEqual({ event: 'request_changes', message: 'Look again.' });
});

test('a manager names the project’s judge from the endpoint’s judges and clears it, and a judge the endpoint does not serve stays to be cleared', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Manager');
	await page.goto('/projects/letters?tab=settings');
	const judge = page.getByLabel('Judge', { exact: true });
	const choose = async (name: string): Promise<void> => {
		await judge.click();
		await page.getByRole('option', { name, exact: true }).click();
	};
	// The models that judge labels, and no other.
	await judge.click();
	await expect(page.getByRole('option')).toHaveText([
		'No judge',
		'Stand-in that never answers (stand-in-slow)',
		'Stand-in that fails (stand-in-failing)',
		'Stand-in judge (stand-in-judge)',
	]);
	await page.keyboard.press('Escape');
	const patched = () =>
		page.waitForRequest(
			(req) => req.method() === 'PATCH' && req.url().endsWith('/api/annotator/projects/letters'),
		);

	let sent = patched();
	await choose('Stand-in judge (stand-in-judge)');
	await page.getByTestId('settings-save').click();
	expect((await sent).postDataJSON()).toEqual({ judge_model: 'stand-in-judge' });
	await expect(page.getByTestId('settings-notice')).toHaveText('Saved.');

	sent = patched();
	await choose('No judge');
	await page.getByTestId('settings-save').click();
	expect((await sent).postDataJSON()).toEqual({ judge_model: '' });

	// With no model endpoint, why is said, and the judge the project names is still offered so it can be cleared.
	await faultOn(page, who, '/api/assist/models', 'no-endpoint');
	await page.goto('/projects/vetting?tab=settings');
	await expect(page.getByTestId('settings-judge-reason')).toHaveText(
		'Assist is unavailable: this deployment names no model endpoint.',
	);
	await expect(judge).toHaveText('stand-in-judge (not served by the model endpoint now)');
	await judge.click();
	await expect(page.getByRole('option')).toHaveText([
		'No judge',
		'stand-in-judge (not served by the model endpoint now)',
	]);
});

test('someone who may not change the judge reads the judge the project names, never said to be unserved by models nobody read', async ({
	page,
	context,
}) => {
	await as(context, 'Reviewer');
	await page.goto('/projects/vetting?tab=settings');
	const judge = page.getByLabel('Judge', { exact: true });
	await expect(judge).toHaveText('stand-in-judge');
	await expect(judge).toBeDisabled();
	await expect(page.getByTestId('settings-judge-reason')).toHaveCount(0);
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
	// An address naming an item no longer listed falls back to the first listed item, and says so in the address.
	await page.goto('/projects/sorting?tab=label&at=sorting-theirs');
	await expect(cellOf(page, 'sorting-00')).toHaveAttribute('tabindex', '0');
	await expect(page).toHaveURL(/[?&]at=sorting-00/);
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
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 2 of 2 (Toggled map): 2 changed.',
	);
	await expect(page.getByTestId('grid-redo')).toHaveAttribute(
		'aria-keyshortcuts',
		'Control+Shift+Z Control+Y',
	);
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
	const refused = cellOf(page, 'sorting-mine').getByTestId('grid-cell-refused');
	await expect(refused).toBeVisible();
	// A refusal longer than the cell's two lines is read whole on the cell in focus.
	await cellOf(page, 'sorting-mine').focus();
	expect(await refused.evaluate((el) => el.scrollHeight <= el.clientHeight + 1)).toBe(true);
	// The item moves on in the other tab (claimed again there): the refusal said of it no longer stands.
	await other.goto('/projects/sorting?tab=label');
	await other.locator('[data-testid="grid-cell"][data-task="sorting-mine"]').click();
	await other.getByTestId('grid-bulk-claim').click();
	await expect(other.getByTestId('grid-notice')).toHaveText('Claimed: 1 item.');
	await page.getByRole('button', { name: 'Refresh' }).click();
	await expect(cellOf(page, 'sorting-mine').getByText('claimed', { exact: true })).toBeVisible();
	await expect(refused).toHaveCount(0);
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

	await expect(page.getByTestId('grid-notice')).toHaveText('Submitted for review: 4 items.');
	await expect(grid(page).getByRole('gridcell')).toHaveCount(21);
	// The item in focus (the third) left the list: the focus stays at its place, on the item now there, and the address
	// names that one, so a person working by keyboard goes on where they were rather than from the top.
	const third = grid(page).getByRole('gridcell').nth(2);
	await expect(third).toHaveAttribute('tabindex', '0');
	await expect(page).toHaveURL(new RegExp(`[?&]at=${await third.getAttribute('data-task')}`));
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
	const form = page.getByTestId('grid-send-back');
	// An empty form closes at once on Escape.
	await page.getByTestId('grid-bulk-request_changes').click();
	await page.keyboard.press('Escape');
	await expect(form).toHaveCount(0);
	// A typed note is not lost by Escape: the form asks, and a second Escape goes back to writing.
	await page.getByTestId('grid-bulk-request_changes').click();
	const note = page.getByRole('textbox', { name: 'What should change' });
	await note.fill('Which page is this?');
	await page.keyboard.press('Escape');
	await expect(form.getByTestId('send-back-discard')).toHaveText(/Discard the note you wrote\?/);
	await page.keyboard.press('Escape');
	await expect(form.getByTestId('send-back-discard')).toHaveCount(0);
	await expect(note).toHaveValue('Which page is this?');
	await form.getByRole('button', { name: 'Send back' }).click();
	await expect(grid(page).getByRole('gridcell')).toHaveCount(1);
	await expect(page.getByTestId('grid-notice')).toHaveText('Sent back: 1 item.');

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
	// Only the item whose `letter` carries words is asked about: setting `map` where a model proposed it confirms it. The
	// question names what is asked, before anything is labelled.
	await expect(page.getByTestId('grid-ask').getByRole('heading')).toHaveText(
		'Label map: replace what other labels carry?',
	);
	await expect(page.getByTestId('grid-ask-count')).toContainText('1 of 3 items carries');
	// Skipping is the default: the question opens on it, and Enter takes it.
	await expect(page.getByTestId('grid-ask-skip')).toBeFocused();
	await page.keyboard.press('Enter');

	await expect(page.getByTestId('grid-notice')).toContainText(
		'Labelled map: 2 changed, 1 not attempted.',
	);
	await expect(labelsOf(page, 'sorting-20')).toHaveText('letter');
	await expect(cellOf(page, 'sorting-20').getByTestId('grid-cell-skipped')).toHaveText(
		'not attempted, kept as it is, with what its label carries',
	);
	// The cell's accessible name says it in the same words.
	await expect(cellOf(page, 'sorting-20')).toHaveAttribute(
		'aria-label',
		/; not attempted, kept as it is, with what its label carries/,
	);
	await expect(labelsOf(page, 'sorting-21')).toHaveText('map');
	await expect(cellOf(page, 'sorting-21').getByTestId('grid-cell-suggested')).toHaveCount(0);

	await cellOf(page, 'sorting-20').click();
	await page.keyboard.press('2');
	// Every item asked about is one that carries more: the question names no rest.
	await expect(page.getByTestId('grid-ask-count')).toContainText(
		'Skipping keeps it as it is, and labels nothing.',
	);
	await page.getByTestId('grid-ask-replace').click();
	await expect(labelsOf(page, 'sorting-20')).toHaveText('map');
	await expect(cellOf(page, 'sorting-20').getByTestId('grid-cell-details')).toHaveCount(0);
	// A key belongs to the question while it is on the page (`walk-keys.ts`), so the undo waits for it to go.
	await expect(page.getByTestId('grid-ask')).toHaveCount(0);
	await page.keyboard.press('Control+z');
	await expect(labelsOf(page, 'sorting-20')).toHaveText('letter');
	await expect(cellOf(page, 'sorting-20').getByTestId('grid-cell-details')).toBeVisible();
});

test('the grid keeps and removes an item’s proposals, each one action its undo takes back, and its submit names an item a proposal holds back', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	const proposals = cellOf(page, 'sorting-21').getByTestId('grid-cell-proposals');
	await expect(proposals).toHaveText('1 proposal to keep or remove');
	await cellOf(page, 'sorting-21').focus();
	await expect(page.getByTestId('grid-proposals-count')).toHaveText(
		'The item in focus holds 1 proposal',
	);

	// Alt+A keeps it, claiming the item first: the model's `map` is the person's label now.
	await page.keyboard.press('Alt+a');
	await expect(page.getByTestId('grid-notice')).toHaveText('Kept the proposals: 1 changed.');
	await expect(labelsOf(page, 'sorting-21')).toHaveText('map');
	await expect(proposals).toHaveCount(0);
	await page.keyboard.press('Control+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 1 of 1 (Kept the proposals): 1 changed.',
	);
	await expect(cellOf(page, 'sorting-21').getByTestId('grid-cell-suggested')).toHaveText('map?');
	await expect(proposals).toHaveText('1 proposal to keep or remove');
	// The claim the keep took is given back with it.
	await expect(cellOf(page, 'sorting-21').getByText('unassigned', { exact: true })).toBeVisible();

	// Claimed, it is offered for submitting, and the submit names the proposal rather than sending it.
	await cellOf(page, 'sorting-21').click();
	await page.getByTestId('grid-bulk-claim').click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Claimed: 1 item.');
	await cellOf(page, 'sorting-21').click();
	await page.getByTestId('grid-bulk-submit').click();
	await expect(page.getByTestId('grid-refusals')).toContainText(
		'1 proposal on this item is neither kept nor removed: keep or remove it before submitting (1)',
	);
	const events = (await annotatorCalls(page, who)).filter(
		(c) => c.method === 'POST' && c.path === '/tasks/sorting-21/events',
	);
	// The keep's claim, the undo's release, the claim; the submit is not sent.
	expect(events.map((c) => c.status)).toEqual([200, 200, 200]);

	// Removed, it is gone, and the item is submitted.
	await cellOf(page, 'sorting-21').focus();
	await page.keyboard.press('Alt+r');
	await expect(page.getByTestId('grid-notice')).toHaveText('Removed the proposals: 1 changed.');
	await expect(labelsOf(page, 'sorting-21')).toHaveText('no label');
	await cellOf(page, 'sorting-21').click();
	await page.getByTestId('grid-bulk-submit').click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Submitted for review: 1 item.');
});

// ASKING A MODEL FROM THE GRID: each selected item asked through the canvas's own assist door, its answer saved to it as
// proposals through the canvas's save door.

/** Every ask for proposals the page sends, as the browser sent it. */
function assistAsks(page: Page): Request[] {
	const asked: Request[] = [];
	page.on('request', (req) => {
		const path = new URL(req.url()).pathname;
		if (path.startsWith('/api/annotator/api/assist/') && req.method() === 'POST') asked.push(req);
	});
	return asked;
}
const UNITS = {
	'sorting-00': 'letter-001/0',
	'sorting-01': 'letter-001/1',
	'sorting-02': 'letter-002/0',
	'sorting-03': 'map-003/0',
	'sorting-mine': 'map-003/0',
} as const;
/** The name of the grid's ``n``th open item (`sorting-<n>`), on the four pages the mock dataset has in turn. */
const openItemName = (n: number): string =>
	[
		'A letter from the harbour · page 1',
		'A letter from the harbour · page 2',
		'A letter about the harvest · page 1',
		'A map of the harbour · page 1',
	][n % 4] ?? '';
/** A save to ``unit``'s work reaches the annotator and lands, and its answer never reaches the page. */
const loseAnswer = (page: Page, unit: string) =>
	page.route(new RegExp(`/api/annotator/api/annotations/${unit}\\?`), async (route) => {
		if (route.request().method() !== 'POST') return route.fallback();
		await route.fetch();
		await route.abort('connectionreset');
	});
/** Open the grid's models and read them. */
async function openAsk(page: Page): Promise<Locator> {
	await page.getByTestId('grid-assist-open').click();
	return page.getByTestId('grid-assist-panel');
}
const gridModel = (panel: Locator, name: string): Locator =>
	panel.locator(`[data-testid="grid-assist-model"][data-model="${name}"]`);
const faultOn = (page: Page, who: MockIdentity, path: string, kind: Fault) =>
	page.request.post(
		`${EDGE}/api/viewer/__fault?${new URLSearchParams({ sub: who.sub, path, kind })}`,
	);

/** The task events the page fires, by item, in the order sent. */
function taskEvents(page: Page): [string, string][] {
	const fired: [string, string][] = [];
	page.on('request', (req) => {
		const at = /\/tasks\/([^/]+)\/events$/.exec(new URL(req.url()).pathname);
		if (req.method() === 'POST' && at?.[1]) fired.push([at[1], String(req.postDataJSON().event)]);
	});
	return fired;
}

test('a model asked over a selection proposes on each item with its model, version and score, and undo takes each save back and gives back the claim it took on an open item', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	const asked = assistAsks(page);
	const fired = taskEvents(page);
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Shift+ArrowRight');
	await page.keyboard.press('Shift+ArrowRight');
	// The reader's own claim is asked about too, and stays theirs whatever is undone.
	await cellOf(page, 'sorting-mine').click({ modifiers: ['Control'] });
	const panel = await openAsk(page);
	await expect(panel.getByRole('heading')).toHaveText('Ask a model about 4 items');

	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();

	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 4 answered.');
	const open = ['sorting-00', 'sorting-01', 'sorting-02'] as const;
	const all = [...open, 'sorting-mine'] as const;
	// Each item asked on its own unit, with the item alone: the tagger takes no prompt.
	expect(asked.map((r) => [new URL(r.url()).pathname, r.postDataJSON()]).sort()).toEqual(
		all.map((id) => [
			`/api/annotator/api/assist/${UNITS[id]}`,
			{ model: 'stand-in-tagger', prompt: null, region: null, points: [], task_id: id },
		]),
	);
	for (const id of all) {
		await expect(cellOf(page, id).getByTestId('grid-cell-assisted')).toHaveText(
			'stand-in-tagger stand-in.1 proposed letter 0.60',
		);
		await expect(cellOf(page, id).getByTestId('grid-cell-suggested')).toHaveText('letter?');
		await expect(cellOf(page, id).getByTestId('grid-cell-proposals')).toHaveText(
			'1 proposal to keep or remove',
		);
	}
	// The open items claimed first, then one save each through the canvas's door.
	expect(fired.toSorted()).toEqual(open.map((id) => [id, 'claim']));
	const saves = (await annotatorCalls(page, who)).filter(
		(c) => c.method === 'POST' && c.path.startsWith('/api/annotations/'),
	);
	expect(saves.map((c) => c.status)).toEqual([200, 200, 200, 200]);
	await expect(cellOf(page, 'sorting-00').getByText('claimed', { exact: true })).toBeVisible();

	// The keyboard is back in the grid. Undo takes the ask back item by item: each holds what it held before it was
	// asked, and an item claimed only to be asked about is given back.
	fired.length = 0;
	await page.keyboard.press('Control+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 1 of 1 (Asked Stand-in tagger): 4 changed.',
	);
	for (const id of all) {
		await expect(labelsOf(page, id)).toHaveText('no label');
		await expect(cellOf(page, id).getByTestId('grid-cell-suggested')).toHaveCount(0);
		await expect(cellOf(page, id).getByTestId('grid-cell-proposals')).toHaveCount(0);
	}
	for (const id of open)
		await expect(cellOf(page, id).getByText('unassigned', { exact: true })).toBeVisible();
	await expect(cellOf(page, 'sorting-mine').getByText('claimed', { exact: true })).toBeVisible();
	expect(fired.toSorted()).toEqual(open.map((id) => [id, 'release']));
	const released = (await annotatorCalls(page, who)).filter(
		(c) => c.method === 'POST' && /^\/tasks\/sorting-0[0-2]\/events$/.test(c.path),
	);
	expect(released.map((c) => c.status)).toEqual([200, 200, 200, 200, 200, 200]);

	// Redone, each open item is claimed again as the ask claimed it, and the proposals are back.
	fired.length = 0;
	await page.keyboard.press('Control+Shift+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Redone step 1 of 1 (Asked Stand-in tagger): 4 changed.',
	);
	for (const id of all)
		await expect(cellOf(page, id).getByTestId('grid-cell-proposals')).toHaveText(
			'1 proposal to keep or remove',
		);
	for (const id of open)
		await expect(cellOf(page, id).getByText('claimed', { exact: true })).toBeVisible();
	expect(fired.toSorted()).toEqual(open.map((id) => [id, 'claim']));
});

test('asking the same model again replaces its proposals on the item in one save that one undo takes back, and another model’s are kept', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	const item = cellOf(page, 'sorting-21');
	// A model of its own proposed `map` on the item before.
	await expect(item.getByTestId('grid-cell-proposals')).toHaveText('1 proposal to keep or remove');
	await item.focus();
	const ask = async (): Promise<void> => {
		const panel = await openAsk(page);
		await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
		await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 1 answered.');
	};

	await ask();
	await expect(item.getByTestId('grid-cell-proposals')).toHaveText('2 proposals to keep or remove');
	await expect(item.getByTestId('grid-cell-suggested')).toHaveText(['letter?', 'map?']);
	await ask();
	await expect(item.getByTestId('grid-cell-proposals')).toHaveText('2 proposals to keep or remove');
	// Asked again and finding nothing, the model's earlier proposals stay, and the item says so.
	await page.route(/\/api\/annotator\/api\/assist\/(?!models)/, (route) =>
		route.fulfill({
			json: { model: 'stand-in-tagger', version: 'stand-in.1', proposals: [], dropped: [] },
		}),
	);
	const panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Asked Stand-in tagger: 0 answered, 1 found nothing.',
	);
	await expect(item.getByTestId('grid-cell-unchanged')).toHaveText(
		'found nothing: its earlier proposals stay',
	);
	await expect(item.getByTestId('grid-cell-proposals')).toHaveText('2 proposals to keep or remove');
	await page.unrouteAll({ behavior: 'wait' });

	await page.keyboard.press('Control+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 2 of 2 (Asked Stand-in tagger): 1 changed.',
	);
	await expect(item.getByTestId('grid-cell-proposals')).toHaveText('2 proposals to keep or remove');
	await expect(item.getByTestId('grid-cell-suggested')).toHaveText(['letter?', 'map?']);
	// Who proposed each is read from what the item holds: it stands after the undo, and after a reload.
	const proposers = 'vlm vlm.3 proposed map 0.91; stand-in-tagger stand-in.1 proposed letter 0.60';
	await expect(item.getByTestId('grid-cell-assisted')).toHaveText(proposers);
	await page.reload();
	await expect(item.getByTestId('grid-cell-assisted')).toHaveText(proposers);
	await item.focus();
	// Kept, the item holds one `letter` and the other model's `map`.
	await page.keyboard.press('Alt+a');
	await expect(page.getByTestId('grid-notice')).toHaveText('Kept the proposals: 1 changed.');
	await expect(item.getByTestId('grid-cell-labels')).toHaveText('letter map');
});

test('the proposals of a whole selection are kept, taken back and removed in one action each', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Shift+ArrowRight');
	await page.keyboard.press('Shift+ArrowRight');
	const panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 3 answered.');
	const asked = ['sorting-00', 'sorting-01', 'sorting-02'];

	await page.keyboard.press('Alt+a');
	await expect(page.getByTestId('grid-notice')).toHaveText('Kept the proposals: 3 changed.');
	for (const id of asked) {
		await expect(labelsOf(page, id)).toHaveText('letter');
		await expect(cellOf(page, id).getByTestId('grid-cell-proposals')).toHaveCount(0);
	}
	await page.keyboard.press('Control+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 2 of 2 (Kept the proposals): 3 changed.',
	);
	for (const id of asked)
		await expect(cellOf(page, id).getByTestId('grid-cell-suggested')).toHaveText('letter?');
	await page.keyboard.press('Alt+r');
	await expect(page.getByTestId('grid-notice')).toHaveText('Removed the proposals: 3 changed.');
	for (const id of asked) {
		await expect(labelsOf(page, id)).toHaveText('no label');
		await expect(cellOf(page, id).getByTestId('grid-cell-proposals')).toHaveCount(0);
	}
});

test('an undo keeps the claim it took on an item that had been sent back, and says why', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const fired = taskEvents(page);
	await page.goto('/projects/batch?tab=label&size=50');
	await cellOf(page, 'batch-sent-back').click();
	await cellOf(page, 'batch-00').click({ modifiers: ['Control'] });
	const panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 2 answered.');
	expect(fired.toSorted()).toEqual([
		['batch-00', 'claim'],
		['batch-sent-back', 'claim'],
	]);

	fired.length = 0;
	await page.keyboard.press('Control+z');
	await expect(page.getByTestId('grid-notice').locator('[data-tone]')).toHaveText(
		'Undone step 1 of 1 (Asked Stand-in tagger): 1 changed, 1 stays claimed by you.',
	);
	await expect(page.getByTestId('grid-stays').getByRole('listitem')).toHaveText([
		'it stays claimed by you: it had been sent back, and releasing it would not send it back again (1): A letter about the harvest · page 1',
	]);
	const sentBack = cellOf(page, 'batch-sent-back');
	await expect(sentBack.getByText('claimed', { exact: true })).toBeVisible();
	await expect(sentBack.getByTestId('grid-cell-proposals')).toHaveCount(0);
	await expect(cellOf(page, 'batch-00').getByText('unassigned', { exact: true })).toBeVisible();
	expect(fired).toEqual([['batch-00', 'release']]);
});

test('a release refused after an undo is said on the item, which stays claimed by you', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.route(/\/api\/annotator\/tasks\/sorting-00\/events$/, (route) =>
		route.request().postDataJSON().event === 'release'
			? route.fulfill({ status: 409, json: { detail: 'the claim could not be released now' } })
			: route.fallback(),
	);
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	const panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 1 answered.');

	await page.keyboard.press('Control+z');

	await expect(page.getByTestId('grid-notice').locator('[data-tone]')).toHaveText(
		'Undone step 1 of 1 (Asked Stand-in tagger): 0 changed, 1 stays claimed by you.',
	);
	await expect(page.getByTestId('grid-stays').getByRole('listitem')).toHaveText([
		'it stays claimed by you (the claim could not be released now) (1): A letter from the harbour · page 1',
	]);
	const item = cellOf(page, 'sorting-00');
	await expect(item.getByTestId('grid-cell-stays')).toHaveText(
		'it stays claimed by you (the claim could not be released now)',
	);
	await expect(item.getByText('claimed', { exact: true })).toBeVisible();
	await expect(item.getByTestId('grid-cell-proposals')).toHaveCount(0);
});

test('an undo after the person released the item and claimed it again leaves their claim alone', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const fired = taskEvents(page);
	await page.goto('/projects/sorting?tab=label');
	const item = cellOf(page, 'sorting-00');
	await item.focus();
	const panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 1 answered.');
	await item.click();
	await page.getByTestId('grid-bulk-release').click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Released: 1 item.');
	await item.click();
	await page.getByTestId('grid-bulk-claim').click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Claimed: 1 item.');

	fired.length = 0;
	await item.focus();
	await page.keyboard.press('Control+z');

	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 1 of 1 (Asked Stand-in tagger): 1 changed.',
	);
	await expect(item.getByTestId('grid-cell-proposals')).toHaveCount(0);
	await expect(item.getByText('claimed', { exact: true })).toBeVisible();
	expect(fired).toEqual([]);
});

/** Have ``who`` let go of their claim of ``task`` and take it again away from the grid (`reclaimElsewhere`). */
const reclaimElsewhere = (page: Page, who: MockIdentity, task: string) =>
	page.request.post(`${EDGE}/api/viewer/__reclaim?${new URLSearchParams({ sub: who.sub, task })}`);

test('an undo leaves alone a claim the person let go and took again away from the grid', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	const fired = taskEvents(page);
	await page.goto('/projects/sorting?tab=label');
	const item = cellOf(page, 'sorting-00');
	await item.focus();
	const panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 1 answered.');
	// In another tab, say: the claim the ask took is let go, and the item claimed again.
	expect((await reclaimElsewhere(page, who, 'sorting-00')).status()).toBe(200);

	fired.length = 0;
	await page.keyboard.press('Control+z');

	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 1 of 1 (Asked Stand-in tagger): 1 changed.',
	);
	await expect(item.getByTestId('grid-cell-proposals')).toHaveCount(0);
	await expect(item.getByText('claimed', { exact: true })).toBeVisible();
	expect(fired).toEqual([]);
});

/** Move ``task`` on away from the grid, its listing not read again (`moveElsewhere`). */
const moveElsewhere = (page: Page, who: MockIdentity, task: string, to: Elsewhere) =>
	page.request.post(`${EDGE}/api/viewer/__move?${new URLSearchParams({ sub: who.sub, task, to })}`);
/** Change ``task``'s work away from the grid, its listing not read again (`changeWorkElsewhere`). */
const changeWorkElsewhere = (page: Page, who: MockIdentity, task: string, change: WorkElsewhere) =>
	page.request.post(
		`${EDGE}/api/viewer/__work?${new URLSearchParams({ sub: who.sub, task, change })}`,
	);
/** Every save to ``unit``'s work answered as one built on a revision the work has moved from. */
const refuseSaves = (page: Page, unit: string) =>
	page.route(new RegExp(`/api/annotator/api/annotations/${unit}\\?`), (route) =>
		route.request().method() === 'POST'
			? route.fulfill({ status: 409, json: { detail: 'annotations changed on the server' } })
			: route.fallback(),
	);

test('whether a claim the grid took goes back is decided by the claim’s own answer, never by the listing read before', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	const fired = taskEvents(page);

	// Read as open, it was claimed, submitted and sent back elsewhere: its claim stays, and says why.
	await page.goto('/projects/sorting?tab=label');
	await expect(cellOf(page, 'sorting-00').getByText('unassigned', { exact: true })).toBeVisible();
	expect((await moveElsewhere(page, who, 'sorting-00', 'changes_requested')).status()).toBe(200);
	await refuseSaves(page, UNITS['sorting-00']);
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('1');
	await expect(page.getByTestId('grid-refusals').getByRole('listitem')).toHaveText([
		'not labelled: annotations changed on the server; it stays claimed by you: it had been sent back, and releasing it would not send it back again (1): A letter from the harbour · page 1',
	]);
	await expect(cellOf(page, 'sorting-00').getByText('claimed', { exact: true })).toBeVisible();
	expect(fired).toEqual([['sorting-00', 'claim']]);
	await page.unrouteAll({ behavior: 'wait' });

	// Read as sent back, it was claimed and let go elsewhere: open now, its claim goes back, and nothing says otherwise.
	fired.length = 0;
	await page.goto('/projects/batch?tab=label&size=50');
	await expect(cellOf(page, 'batch-sent-back').getByText('changes requested')).toBeVisible();
	expect((await moveElsewhere(page, who, 'batch-sent-back', 'unassigned')).status()).toBe(200);
	await refuseSaves(page, 'letter-002/0');
	await cellOf(page, 'batch-sent-back').focus();
	await page.keyboard.press('1');
	await expect(page.getByTestId('grid-refusals').getByRole('listitem')).toHaveText([
		'not labelled, and released again: annotations changed on the server (1): A letter about the harvest · page 1',
	]);
	expect(fired).toEqual([
		['batch-sent-back', 'claim'],
		['batch-sent-back', 'release'],
	]);
	await page.unrouteAll({ behavior: 'wait' });
});

test('a label or a keep that finds what it asked already so gives back the claim it took for it', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	const fired = taskEvents(page);
	await page.goto('/projects/sorting?tab=label');
	await expect(cellOf(page, 'sorting-00').getByText('unassigned', { exact: true })).toBeVisible();
	await expect(cellOf(page, 'sorting-21').getByTestId('grid-cell-proposals')).toBeVisible();
	// Elsewhere, the item is labelled `letter`; the grid has not read it again.
	expect((await changeWorkElsewhere(page, who, 'sorting-00', 'tag-letter')).status()).toBe(200);

	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('1');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Labelled letter: 0 changed, 1 already so.',
	);
	await expect(cellOf(page, 'sorting-00').getByText('unassigned', { exact: true })).toBeVisible();
	// Elsewhere, the other model's proposal is removed; the grid has not read it again.
	expect((await changeWorkElsewhere(page, who, 'sorting-21', 'remove-proposals')).status()).toBe(
		200,
	);
	await cellOf(page, 'sorting-21').focus();
	await page.keyboard.press('Alt+a');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Kept the proposals: 0 changed, 1 already so.',
	);
	await expect(cellOf(page, 'sorting-21').getByText('unassigned', { exact: true })).toBeVisible();
	expect(fired).toEqual([
		['sorting-00', 'claim'],
		['sorting-00', 'release'],
		['sorting-21', 'claim'],
		['sorting-21', 'release'],
	]);
});

test('a redo that claims again and whose undo is then refused gives the claim back, and says so', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const fired = taskEvents(page);
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	const panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 1 answered.');
	await page.keyboard.press('Control+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 1 of 1 (Asked Stand-in tagger): 1 changed.',
	);
	// In another tab, the item is labelled and let go: its work has moved on from what the undo wrote.
	const other = await context.newPage();
	await other.goto('/projects/sorting?tab=label');
	const there = other.locator('[data-testid="grid-cell"][data-task="sorting-00"]');
	await there.focus();
	await other.keyboard.press('1');
	await expect(there.getByTestId('grid-cell-labels')).toHaveText('letter');
	await there.click();
	await other.getByTestId('grid-bulk-release').click();
	await expect(other.getByTestId('grid-notice')).toHaveText('Released: 1 item.');
	await other.close();

	fired.length = 0;
	await page.bringToFront();
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Control+Shift+z');

	await expect(page.getByTestId('grid-notice').locator('[data-tone]')).toHaveText(
		'Redone step 1 of 1 (Asked Stand-in tagger): 0 changed, 1 refused.',
	);
	await expect(page.getByTestId('grid-refusals').getByRole('listitem')).toHaveText([
		'not redone, and released again: annotations changed on the server (loaded v2, now v3) (1): A letter from the harbour · page 1',
	]);
	expect(fired).toEqual([
		['sorting-00', 'claim'],
		['sorting-00', 'release'],
	]);
});

test('an item asked twice is undone one ask after another back to as it was, its claim given back with the first, and redone the same way', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const fired = taskEvents(page);
	await page.goto('/projects/sorting?tab=label');
	const item = cellOf(page, 'sorting-00');
	await item.focus();
	const ask = async (): Promise<void> => {
		const panel = await openAsk(page);
		await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
		await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 1 answered.');
	};
	await ask();
	await ask();
	await expect(item.getByTestId('grid-cell-proposals')).toHaveText('1 proposal to keep or remove');
	expect(fired).toEqual([['sorting-00', 'claim']]);

	fired.length = 0;
	await page.keyboard.press('Control+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 2 of 2 (Asked Stand-in tagger): 1 changed.',
	);
	await expect(item.getByTestId('grid-cell-proposals')).toHaveText('1 proposal to keep or remove');
	await expect(item.getByText('claimed', { exact: true })).toBeVisible();
	expect(fired).toEqual([]);
	await page.keyboard.press('Control+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 1 of 2 (Asked Stand-in tagger): 1 changed.',
	);
	await expect(item.getByTestId('grid-cell-proposals')).toHaveCount(0);
	await expect(labelsOf(page, 'sorting-00')).toHaveText('no label');
	await expect(item.getByText('unassigned', { exact: true })).toBeVisible();
	expect(fired).toEqual([['sorting-00', 'release']]);

	fired.length = 0;
	await page.keyboard.press('Control+Shift+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Redone step 1 of 2 (Asked Stand-in tagger): 1 changed.',
	);
	await expect(item.getByText('claimed', { exact: true })).toBeVisible();
	await page.keyboard.press('Control+Shift+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Redone step 2 of 2 (Asked Stand-in tagger): 1 changed.',
	);
	await expect(item.getByTestId('grid-cell-proposals')).toHaveText('1 proposal to keep or remove');
	expect(fired).toEqual([['sorting-00', 'claim']]);
});

test('labels written twice over a selection are undone twice and redone twice, each step on every item', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Shift+ArrowRight');
	await page.keyboard.press('Shift+ArrowRight');
	const items = ['sorting-00', 'sorting-01', 'sorting-02'];
	const labelled = async (text: string): Promise<void> => {
		for (const id of items) await expect(labelsOf(page, id)).toHaveText(text);
	};
	const notice = page.getByTestId('grid-notice');
	// Both directions are named where the keys are said.
	await expect(page.getByTestId('grid-keys-help')).toHaveText(
		'Arrows move, Shift with an arrow selects, Space picks one, Ctrl+A the page; 1–9 label the selection (or the item in focus, then the next), Alt with a digit adds a label beside the others, Delete clears, Alt+A keeps and Alt+R removes every proposal, Ctrl+Z undoes and Ctrl+Shift+Z or Ctrl+Y redoes, Enter opens the item on the canvas, Escape clears the selection.',
	);

	await page.keyboard.press('1');
	await expect(notice).toHaveText('Labelled letter: 3 changed.');
	await page.keyboard.press('2');
	await expect(notice).toHaveText('Labelled map: 3 changed.');
	await labelled('map');

	await page.keyboard.press('Control+z');
	await expect(notice).toHaveText('Undone step 2 of 2 (Labelled map): 3 changed.');
	await labelled('letter');
	await page.keyboard.press('Control+z');
	await expect(notice).toHaveText('Undone step 1 of 2 (Labelled letter): 3 changed.');
	await labelled('no label');
	await page.keyboard.press('Control+Shift+z');
	await expect(notice).toHaveText('Redone step 1 of 2 (Labelled letter): 3 changed.');
	await labelled('letter');
	await page.keyboard.press('Control+Shift+z');
	await expect(notice).toHaveText('Redone step 2 of 2 (Labelled map): 3 changed.');
	await labelled('map');
});

test('an undo refused because someone else changed the item is said on it, and the steps after it stay undone and redone', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('1');
	await expect(labelsOf(page, 'sorting-00')).toHaveText('letter');
	await expect(cellOf(page, 'sorting-01')).toBeFocused();
	await page.keyboard.press('2');
	await expect(labelsOf(page, 'sorting-01')).toHaveText('map');
	// Elsewhere, sorting-00's work changes after the label.
	expect((await changeWorkElsewhere(page, who, 'sorting-00', 'tag-letter')).status()).toBe(200);
	const notice = page.getByTestId('grid-notice');

	await page.keyboard.press('Control+z');
	await expect(notice).toHaveText('Undone step 2 of 2 (Labelled map): 1 changed.');
	await expect(labelsOf(page, 'sorting-01')).toHaveText('no label');
	await page.keyboard.press('Control+z');
	await expect(notice.locator('[data-tone]')).toHaveText(
		'Undone step 1 of 2 (Labelled letter): 0 changed, 1 refused.',
	);
	await expect(page.getByTestId('grid-refusals').getByRole('listitem')).toHaveText([
		'not undone: annotations changed on the server (loaded v1, now v2) (1): A letter from the harbour · page 1',
	]);
	await expect(cellOf(page, 'sorting-00').getByTestId('grid-cell-refused')).toHaveText(
		'⚠ refused, not undone: annotations changed on the server (loaded v1, now v2)',
	);
	await page.keyboard.press('Control+Shift+z');
	await expect(notice).toHaveText('Redone step 1 of 1 (Labelled map): 1 changed.');
	await expect(labelsOf(page, 'sorting-01')).toHaveText('map');
});

test('a save waits on no read past Stop or past leaving the page: the claim goes back and nothing is written', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const fired = taskEvents(page);
	const saves: string[] = [];
	page.on('request', (req) => {
		const url = new URL(req.url());
		if (req.method() === 'POST' && url.pathname.startsWith('/api/annotator/api/annotations/'))
			saves.push(url.searchParams.get('task') ?? '');
	});
	// The read of sorting-21's rows (it holds a model's proposal) is held until the test lets it go.
	const holdRead = async (): Promise<Route[]> => {
		const held: Route[] = [];
		await page.route(/\/api\/annotator\/api\/annotations\/[^?]+\?.*task=sorting-21/, (route) =>
			route.request().method() === 'GET' ? held.push(route) : route.fallback(),
		);
		return held;
	};

	// Stopped while the read is out.
	let held = await holdRead();
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-20').click();
	await cellOf(page, 'sorting-21').click({ modifiers: ['Control'] });
	let panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect.poll(() => held.length).toBe(1);
	await page.getByTestId('grid-stop').click();
	await expect(page.getByTestId('grid-skipped').getByRole('listitem')).toHaveText([
		'Not attempted, stopped while it was asked, and released again (1): A letter from the harbour · page 2',
	]);
	await Promise.all(held.map((r) => r.continue().catch(() => {})));
	expect(saves).toEqual(['sorting-20']);
	expect(fired.filter(([id]) => id === 'sorting-21')).toEqual([
		['sorting-21', 'claim'],
		['sorting-21', 'release'],
	]);
	await page.unrouteAll({ behavior: 'ignoreErrors' });

	// The page left while the read is out.
	fired.length = 0;
	held = await holdRead();
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-21').focus();
	panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect.poll(() => held.length).toBe(1);
	await page.getByTestId('tab-items').click();
	await expect(grid(page)).toHaveCount(0);
	await Promise.all(held.map((r) => r.continue().catch(() => {})));
	await expect
		.poll(() => fired)
		.toEqual([
			['sorting-21', 'claim'],
			['sorting-21', 'release'],
		]);
	expect(saves).toEqual(['sorting-20']);
	await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('a model that takes words asks every selected item with the one prompt, and one that needs a box or clicks is not offered', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const asked = assistAsks(page);
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Shift+ArrowRight');
	const panel = await openAsk(page);
	// The segmenter takes only a box or clicks, drawn on one item: the canvas's.
	await expect(gridModel(panel, 'stand-in-detector')).toBeVisible();
	await expect(gridModel(panel, 'stand-in-tagger')).toBeVisible();
	await expect(gridModel(panel, 'stand-in-segmenter')).toHaveCount(0);
	const find = gridModel(panel, 'stand-in-detector').getByRole('button', { name: 'Find' });
	await expect(find).toBeDisabled();

	// Enter in the words asks, with the first model that takes them.
	await panel.getByTestId('grid-assist-prompt').fill('a seal');
	await expect(panel.getByTestId('grid-assist-enter')).toHaveText(
		'Enter finds it with Stand-in detector',
	);
	await panel.getByTestId('grid-assist-prompt').press('Enter');

	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in detector: 2 answered.');
	expect(asked.map((r) => r.postDataJSON().prompt)).toEqual(['a seal', 'a seal']);
	expect(asked.map((r) => r.postDataJSON().region)).toEqual([null, null]);
	await expect(cellOf(page, 'sorting-01').getByTestId('grid-cell-assisted')).toHaveText(
		'stand-in-detector stand-in.1 proposed letter (bbox) 0.70',
	);
	await expect(cellOf(page, 'sorting-01').getByTestId('grid-cell-proposals')).toHaveText(
		'1 proposal to keep or remove',
	);

	// A proposal with no class is told as an unclassed tag, with what keeping it leaves to do.
	await page.route(/\/api\/annotator\/api\/assist\/(?!models)/, (route) =>
		route.fulfill({
			json: {
				model: 'stand-in-tagger',
				version: 'stand-in.1',
				proposals: [
					{
						shape_type: 'tag',
						x: 0,
						y: 0,
						width: 0,
						height: 0,
						polygon: [],
						label: '',
						text: '',
						score: 0.6,
					},
				],
				dropped: [],
			},
		}),
	);
	const again = await openAsk(page);
	await gridModel(again, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 2 answered.');
	await expect(cellOf(page, 'sorting-01').getByTestId('grid-cell-assisted')).toHaveText(
		'stand-in-tagger stand-in.1 proposed a tag with no class yet 0.60 (keeping it leaves a class to pick before submitting)',
	);
	await page.unrouteAll({ behavior: 'wait' });
});

test('a slow, busy or failing model is named on each item it failed for, the answered item keeps its proposal, and the rest are released', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await faultOn(page, who, '/api/assist/letter-001/1', 'slow');
	await faultOn(page, who, '/api/assist/letter-002/0', 'busy');
	await faultOn(page, who, '/api/assist/map-003/0', 'fail');
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	for (let i = 0; i < 3; i++) await page.keyboard.press('Shift+ArrowRight');
	const panel = await openAsk(page);

	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();

	await expect(page.getByTestId('grid-notice').locator('[data-tone]')).toHaveText(
		'Asked Stand-in tagger: 1 answered, 3 refused.',
	);
	// Grouped by what failed; each item's cell keeps the endpoint's own words.
	await expect(page.getByTestId('grid-refusals').getByRole('listitem')).toHaveText([
		'not answered, and released again: the model did not answer in time (1): A letter from the harbour · page 2',
		'not answered, and released again: the model endpoint was busy (1): A letter about the harvest · page 1',
		'not answered, and released again: the model endpoint failed (1): A map of the harbour · page 1',
	]);
	await expect(cellOf(page, 'sorting-01').getByTestId('grid-cell-refused')).toHaveText(
		'⚠ refused, not answered, and released again: the model endpoint did not answer within 30 s',
	);
	await expect(cellOf(page, 'sorting-02').getByTestId('grid-cell-refused')).toHaveText(
		'⚠ refused, not answered, and released again: the model endpoint is busy or unavailable for now',
	);
	await expect(cellOf(page, 'sorting-03').getByTestId('grid-cell-refused')).toHaveText(
		'⚠ refused, not answered, and released again: the model endpoint failed (500)',
	);
	await expect(cellOf(page, 'sorting-00').getByTestId('grid-cell-assisted')).toHaveText(
		'stand-in-tagger stand-in.1 proposed letter 0.60',
	);
	await expect(cellOf(page, 'sorting-00').getByTestId('grid-cell-proposals')).toBeVisible();
	for (const id of ['sorting-01', 'sorting-02', 'sorting-03']) {
		await expect(cellOf(page, id).getByText('unassigned', { exact: true })).toBeVisible();
		await expect(cellOf(page, id).getByTestId('grid-cell-proposals')).toHaveCount(0);
	}
});

test('Stop ends an ask with what is in flight given up and what is still waiting not asked', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	// The model holds every ask: the run waits on the first few.
	const held: Route[] = [];
	await page.route(/\/api\/annotator\/api\/assist\/(?!models)/, (route) => {
		held.push(route);
	});
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Control+a');
	const panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect(page.getByTestId('grid-progress')).toHaveText(
		'Asking Stand-in tagger: 0 of 25… Stop',
	);
	await expect.poll(() => held.length).toBe(4);

	await page.getByTestId('grid-stop').click();

	await expect(page.getByTestId('grid-notice').locator('[data-tone]')).toHaveText(
		'Asked Stand-in tagger: stopped at 0 of 25; 0 answered, 25 not attempted.',
	);
	await expect(page.getByTestId('grid-notice').locator('[data-tone]')).toHaveAttribute(
		'data-tone',
		'stopped',
	);
	await expect(page.getByTestId('grid-skipped').getByRole('listitem')).toHaveText([
		`Not attempted, stopped before it was started (21): ${[4, 5, 6, 7, 8].map(openItemName).join(', ')} and 16 more`,
		`Not attempted, stopped while it was asked, and released again (4): ${[0, 1, 2, 3].map(openItemName).join(', ')}`,
	]);
	expect(held.length).toBe(4);
	await expect(cellOf(page, 'sorting-00').getByText('unassigned', { exact: true })).toBeVisible();
	// No answer landed: no item says the model proposed anything (another model's earlier proposal stays said).
	await expect(
		grid(page).getByTestId('grid-cell-assisted').filter({ hasText: 'stand-in-tagger' }),
	).toHaveCount(0);
	await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('an item taken meanwhile, or whose work moved since it was read, is refused and named, never overwritten', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const asked = assistAsks(page);
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').click();
	await cellOf(page, 'sorting-01').click({ modifiers: ['Control'] });
	await cellOf(page, 'sorting-mine').click({ modifiers: ['Control'] });
	await expect(grid(page).locator('[aria-selected="true"]')).toHaveCount(3);
	// In another tab (the mock keeps one world per person), the item held by the reader is labelled, and an item nobody
	// held is claimed.
	const other = await context.newPage();
	await other.goto('/projects/sorting?tab=label');
	const there = (id: string) => other.locator(`[data-testid="grid-cell"][data-task="${id}"]`);
	await there('sorting-mine').focus();
	await other.keyboard.press('1');
	await expect(there('sorting-mine').getByTestId('grid-cell-labels')).toHaveText('letter');
	await there('sorting-01').click();
	await other.getByTestId('grid-bulk-claim').click();
	await expect(other.getByTestId('grid-notice')).toHaveText('Claimed: 1 item.');

	const panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();

	await expect(page.getByTestId('grid-notice').locator('[data-tone]')).toHaveText(
		'Asked Stand-in tagger: 1 answered, 2 refused.',
	);
	await expect(page.getByTestId('grid-refusals').getByRole('listitem')).toHaveText([
		'not claimed: illegal task transition: claimed --claim--> (1): A letter from the harbour · page 2',
		'annotations changed on the server (loaded v0, now v1) (1): A map of the harbour · page 1',
	]);
	await expect(cellOf(page, 'sorting-mine').getByTestId('grid-cell-refused')).toBeVisible();
	// Neither is overwritten: the item taken meanwhile was never asked, and the moved one keeps what it holds.
	expect(asked.map((r) => r.postDataJSON().task_id).sort()).toEqual(['sorting-00', 'sorting-mine']);
	await expect(labelsOf(page, 'sorting-mine')).toHaveText('letter');
	await expect(cellOf(page, 'sorting-mine').getByTestId('grid-cell-proposals')).toHaveCount(0);
	await expect(cellOf(page, 'sorting-00').getByTestId('grid-cell-assisted')).toHaveText(
		'stand-in-tagger stand-in.1 proposed letter 0.60',
	);
});

test('leaving the page while a model is asked gives up the asks in flight and claims nothing more', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const held: Route[] = [];
	await page.route(/\/api\/annotator\/api\/assist\/(?!models)/, (route) => {
		held.push(route);
	});
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Control+a');
	const panel = await openAsk(page);
	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();
	await expect.poll(() => held.length).toBe(4);
	const events: string[] = [];
	const answered: string[] = [];
	const isEvent = (req: Request): boolean =>
		req.method() === 'POST' && /\/tasks\/[^/]+\/events$/.test(new URL(req.url()).pathname);
	page.on('request', (req) => {
		if (isEvent(req)) events.push(String(req.postDataJSON().event));
	});
	page.on('response', (res) => {
		if (isEvent(res.request())) answered.push(res.url());
	});

	await page.getByTestId('tab-items').click();
	await expect(grid(page)).toHaveCount(0);

	// The four asks in flight give their claims back; no item waiting is claimed, asked or released. Nothing marks the
	// end of work that must not happen, so the page is given a moment in which it would have.
	await expect.poll(() => events).toEqual(['release', 'release', 'release', 'release']);
	await expect.poll(() => answered.length).toBe(4);
	// Every worker of the run reads the stop before it starts an item, and the stop came before these releases, so once
	// they are answered no item is left to be claimed or asked.
	expect(events).toEqual(['release', 'release', 'release', 'release']);
	expect(held.length).toBe(4);
	await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('an endpoint unavailable for an item, and an answer its classes cannot hold whole or in part, are named on each item, and what fits is kept', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	// The tagger answers sorting-00 a polygon beside its tag and sorting-01 a polygon alone: the items take tags and boxes.
	await faultOn(page, who, '/api/assist/letter-001/0', 'mixed');
	await faultOn(page, who, '/api/assist/letter-001/1', 'polygon');
	await faultOn(page, who, '/api/assist/letter-002/0', 'no-endpoint');
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	for (let i = 0; i < 2; i++) await page.keyboard.press('Shift+ArrowRight');
	const panel = await openAsk(page);

	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();

	await expect(page.getByTestId('grid-notice').locator('[data-tone]')).toHaveText(
		'Asked Stand-in tagger: 1 answered, 1 in part, 2 refused.',
	);
	await expect(page.getByTestId('grid-refusals').getByRole('listitem')).toHaveText([
		"nothing it proposed fits, and released again: a shape this item's classes do not take (1): A letter from the harbour · page 2",
		'not answered, and released again: the model endpoint is unavailable (1): A letter about the harvest · page 1',
	]);
	await expect(page.getByTestId('grid-left-out').getByRole('listitem')).toHaveText([
		"Left out of an answer, a shape this item's classes do not take (1): A letter from the harbour · page 1",
	]);
	await expect(cellOf(page, 'sorting-00').getByTestId('grid-cell-assisted')).toHaveText(
		'stand-in-tagger stand-in.1 proposed letter 0.60',
	);
	await expect(cellOf(page, 'sorting-00').getByTestId('grid-cell-left-out')).toHaveText(
		"left out 1 polygon proposal(s): this item's classes take bbox, tag",
	);
	await expect(cellOf(page, 'sorting-00').getByTestId('grid-cell-proposals')).toHaveText(
		'1 proposal to keep or remove',
	);
	await expect(cellOf(page, 'sorting-01').getByTestId('grid-cell-refused')).toHaveText(
		"⚠ refused, nothing it proposed fits, and released again: 1 polygon proposal(s): this item's classes take bbox, tag",
	);
	await expect(cellOf(page, 'sorting-02').getByTestId('grid-cell-refused')).toHaveText(
		'⚠ refused, not answered, and released again: assist is unavailable: this deployment names no model endpoint',
	);
	for (const id of ['sorting-01', 'sorting-02'])
		await expect(cellOf(page, id).getByText('unassigned', { exact: true })).toBeVisible();
});

test('a save whose answer was lost after it landed is reported as saved, keeps its claim, and is undone', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	// sorting-01's save reaches the annotator and lands, and its answer never reaches the page.
	await loseAnswer(page, UNITS['sorting-01']);
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Shift+ArrowRight');
	const panel = await openAsk(page);

	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();

	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 2 answered.');
	const landed = cellOf(page, 'sorting-01');
	await expect(landed.getByTestId('grid-cell-assisted')).toHaveText(
		'stand-in-tagger stand-in.1 proposed letter 0.60',
	);
	await expect(landed.getByTestId('grid-cell-refused')).toHaveCount(0);
	await expect(landed.getByText('claimed', { exact: true })).toBeVisible();

	await page.keyboard.press('Control+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 1 of 1 (Asked Stand-in tagger): 2 changed.',
	);
	await expect(landed.getByTestId('grid-cell-proposals')).toHaveCount(0);
	await expect(landed.getByTestId('grid-cell-suggested')).toHaveCount(0);
	await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('a lost save another save landed on top of is reported saved, and an undo leaves that item as it now is', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	// sorting-01's save lands, another save lands on top of it, and its answer never reaches the page.
	await faultOn(page, who, '/api/annotations/letter-001/1', 'moved-after');
	await loseAnswer(page, UNITS['sorting-01']);
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Shift+ArrowRight');
	const panel = await openAsk(page);

	await gridModel(panel, 'stand-in-tagger').getByRole('button', { name: 'Ask' }).click();

	await expect(page.getByTestId('grid-notice')).toHaveText('Asked Stand-in tagger: 2 answered.');
	const landed = cellOf(page, 'sorting-01');
	await expect(landed.getByTestId('grid-cell-refused')).toHaveCount(0);

	// The undo takes back sorting-00's save alone: naming sorting-01's latest revision would take back the other save.
	await page.keyboard.press('Control+z');
	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 1 of 1 (Asked Stand-in tagger): 1 changed.',
	);
	await expect(landed.getByTestId('grid-cell-proposals')).toHaveText(
		'1 proposal to keep or remove',
	);
	await expect(cellOf(page, 'sorting-00').getByTestId('grid-cell-proposals')).toHaveCount(0);
	await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('a model that only judges labels is never offered to ask from the grid', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	const panel = await openAsk(page);

	await expect(gridModel(panel, 'stand-in-tagger')).toBeVisible();
	await expect(gridModel(panel, 'stand-in-judge')).toHaveCount(0);
});

test('a model no selected item’s classes can hold is said not to fit and cannot be asked', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	// The endpoint serves an outliner too, which answers polygons alone: the items take tags and boxes.
	await page.route(/\/api\/annotator\/api\/assist\/models/, async (route) => {
		const listed = await route.fetch();
		const body = (await listed.json()) as { models: Record<string, unknown>[] };
		body.models.push({
			name: 'stand-in-outliner',
			version: 'stand-in.1',
			title: 'Stand-in outliner',
			reads: ['image'],
			prompts: [],
			returns: ['polygon'],
			assesses: [],
			compatible: null,
		});
		await route.fulfill({ response: listed, json: body });
	});
	await page.goto('/projects/sorting?tab=label');
	await cellOf(page, 'sorting-00').focus();
	await page.keyboard.press('Shift+ArrowRight');
	const panel = await openAsk(page);

	const outliner = gridModel(panel, 'stand-in-outliner');
	await expect(outliner.getByTestId('grid-assist-incompatible')).toHaveText('does not fit');
	await expect(outliner.getByRole('button', { name: 'Ask' })).toBeDisabled();
	const tagger = gridModel(panel, 'stand-in-tagger');
	await expect(tagger.getByTestId('grid-assist-incompatible')).toHaveCount(0);
	await expect(tagger.getByRole('button', { name: 'Ask' })).toBeEnabled();
});

test('a person who may not annotate the project is offered no model to ask from the grid', async ({
	page,
	context,
}) => {
	await as(context, 'Viewer');
	await page.goto('/projects/sorting?tab=label');
	await expect(grid(page).getByRole('gridcell').first()).toBeVisible();
	await expect(page.getByTestId('grid-assist-open')).toHaveCount(0);
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
	await expect(page.getByTestId('grid-ask-count')).toContainText('1 of 25 items carries');
	await expect(page.getByTestId('grid-ask-skip')).toBeFocused();
	await page.keyboard.press('Enter');
	await expect(page.getByTestId('grid-progress')).toContainText('Labelling letter: 0 of 25');
	await page.getByTestId('grid-stop').click();

	// Only the four in flight when Stop was pressed finish, and a stop is no failure.
	await expect(page.getByTestId('grid-notice')).toContainText(
		'Labelled letter: stopped at 0 of 25; 4 changed, 21 not attempted.',
		{ timeout: 10_000 },
	);
	await expect(page.getByTestId('grid-notice').locator('[data-tone]')).toHaveAttribute(
		'data-tone',
		'stopped',
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

	// Its own tag is there: this save landed, and is reported done, its claim kept with it.
	await expect(page.getByTestId('grid-notice')).toHaveText('Labelled letter: 1 changed.');
	await expect(labelsOf(page, 'sorting-00')).toHaveText('letter');
	await expect(cellOf(page, 'sorting-00').getByText('claimed', { exact: true })).toBeVisible();
	await page.unrouteAll({ behavior: 'wait' });
	await cellOf(page, 'sorting-01').focus();
	await page.keyboard.press('1');
	await expect(labelsOf(page, 'sorting-01')).toHaveText('letter');
	await lose(/\/revert\?/);

	await page.keyboard.press('Control+z');

	await expect(page.getByTestId('grid-notice')).toHaveText(
		'Undone step 2 of 2 (Labelled letter): 1 changed.',
	);
	await expect(labelsOf(page, 'sorting-01')).toHaveText('no label');
});

test('select all matching keeps the keyboard in the grid, so the next digit labels every item that matches', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/batch?tab=label&size=25');
	await cellOf(page, 'batch-00').focus();
	await page.keyboard.press('Control+a');

	await page.getByTestId('grid-select-all-matching').click();

	await expect(page.getByTestId('grid-bulk-bar')).toContainText('31 selected');
	await expect(cellOf(page, 'batch-00')).toBeFocused();
	await page.keyboard.press('1');
	await expect(page.getByTestId('grid-notice')).toHaveText('Labelled letter: 31 changed.', {
		timeout: 15_000,
	});
});

test('a reviewer’s note is read on the sent-back item’s grid cell, by whom, whole on the cell in focus', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/batch?tab=label&size=50');
	const onCell = cellOf(page, 'batch-sent-back').getByTestId('grid-cell-note');
	await expect(onCell).toContainText('Alma Alpha: Check the class');
	// Bounded on the cell, whole on the cell in focus.
	expect(await onCell.evaluate((el) => el.scrollHeight > el.clientHeight + 1)).toBe(true);
	await cellOf(page, 'batch-sent-back').focus();
	expect(await onCell.evaluate((el) => el.scrollHeight <= el.clientHeight + 1)).toBe(true);
});

for (const [width, height, theme, mode] of [
	[1440, 900, 'ink', 'dark'],
	[390, 844, 'ink', 'dark'],
	[1440, 900, 'paper', 'light'],
	[390, 844, 'paper', 'light'],
] as const)
	test(`every row action of the items table is in reach at ${width}×${height} in ${theme} ${mode}, with no page scrolled sideways`, async ({
		page,
		context,
	}) => {
		await as(context, 'Manager');
		// The person's look, as the account menu keeps it (`#lib/appearance-options.ts`).
		await page.addInitScript(
			([theme, mode]) =>
				localStorage.setItem(
					'workbench-appearance',
					JSON.stringify({ v: 2, theme, mode, accent: null }),
				),
			[theme, mode] as const,
		);
		await page.setViewportSize({ width, height });
		await letters(page);
		await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
		// The row's actions: its last cell's buttons and links.
		const actions = row(page, 'letter-002/0').getByRole('cell').last().locator('a, button');
		await expect(actions.first()).toBeVisible();
		// The row brought into view with its table scrolled to its start, as a person arriving finds it.
		await row(page, 'letter-002/0')
			.getByRole('cell')
			.first()
			.evaluate((el) => el.scrollIntoView({ block: 'center', inline: 'start' }));

		// Read once the page has settled (a page transition covers it while it runs): each action within the window and
		// the topmost thing at its centre, and the page not wider than the window.
		await expect
			.poll(async () => [
				...(await actions.evaluateAll((elements) =>
					elements.map((el) => {
						const box = el.getBoundingClientRect();
						const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
						return box.right <= window.innerWidth && box.left >= 0 && !!hit && el.contains(hit);
					}),
				)),
				await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
			])
			.toEqual(Array.from({ length: (await actions.count()) + 1 }, () => true));
	});

test('a grid cell’s focus ring is drawn on all four sides, at the row’s first column and its last', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/sorting?tab=label');
	const first = cellOf(page, 'sorting-00');
	await first.focus();

	// The ring is drawn outside the cell; the row it sits in clips what it paints past its own box.
	const room = async (): Promise<boolean[]> =>
		page.evaluate(() => {
			const el = document.activeElement as HTMLElement;
			const ring = parseFloat(
				getComputedStyle(el)
					.getPropertyValue('--tw-ring-shadow')
					.match(/(\d+(\.\d+)?)px/)?.[1] ?? '0',
			);
			const box = el.getBoundingClientRect();
			const row = (el.parentElement as HTMLElement).getBoundingClientRect();
			return [
				ring > 0,
				box.top - row.top >= ring,
				row.bottom - box.bottom >= ring,
				box.left - row.left >= ring,
				row.right - box.right >= ring,
			];
		});
	await page.keyboard.press('ArrowRight');
	await page.keyboard.press('ArrowLeft');
	const atFirst = await room();
	await page.keyboard.press('End');
	await expect(first).not.toBeFocused();
	const atLast = await room();

	expect([atFirst, atLast]).toEqual([Array(5).fill(true), Array(5).fill(true)]);
});

test('the app’s navigation and the grid’s pager are two landmarks of their own names', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/batch?tab=label&size=25');
	await expect(grid(page)).toBeVisible();

	await expect(page.getByRole('navigation', { name: 'Pages', exact: true })).toHaveCount(1);
	await expect(page.getByRole('navigation', { name: 'Pages of items' })).toHaveCount(1);
});

/** A service's own refusal of the session: a 401 that challenges the credential. This is the one a viewer read answers
 *  when the person's session to the lakehouse must be renewed (`token_exchange.SESSION_TO_RENEW`: no unexpired token of
 *  theirs to pass on, or the exchange of it refused), and the door's own carries the same challenge. */
// SAMPLING (ADR 0034): `triage` samples the least sure by the stand-in tagger; the letters dataset's score column scores
// four of its five open items (doubts 0.83, 0.62, 0.47, 0.21) and the tagger, asked about the fifth, answers 0.4.
// `blended` and `fresh` sample the least alike in the letters dataset's embeddings, against one labelled item and none;
// `unscored` mixes the two over the maps dataset, which declares neither (`e2e/mock-projects.ts`).
const samplingAsks = async (page: Page, who: MockIdentity): Promise<Call[]> =>
	(await annotatorCalls(page, who)).filter(
		(c) => c.method === 'POST' && c.path.startsWith('/api/sampling/'),
	);
const gridOrder = (page: Page) =>
	grid(page)
		.getByRole('gridcell')
		.evaluateAll((cells) => cells.map((c) => c.getAttribute('data-task')));

test('with least sure on, the Label and Items tabs list the items doubted most first, each saying why, and plain order restores item order', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await page.goto('/projects/triage?tab=label');

	// The visit's round read the score column, asked the model about the one item it does not score, and read the list
	// again in the strategy's order.
	await expect(page.getByTestId('grid-sampling-line')).toHaveText(
		'Least sure by stand-in-tagger: 5 scored, 0 not yet.',
	);
	expect((await samplingAsks(page, who)).map((c) => [c.path, c.status])).toEqual([
		['/api/sampling/letter-004/0', 200],
	]);
	await expect
		.poll(() => gridOrder(page))
		.toEqual(['triage-01', 'triage-03', 'triage-02', 'triage-04', 'triage-00']);
	await expect(cellOf(page, 'triage-01').getByTestId('sampling-reason')).toHaveText(
		'Least sure · doubt 0.83',
	);
	await expect(cellOf(page, 'triage-01').getByTestId('sampling-chosen')).toHaveText(
		'Chosen by sampling: least sure (0.83)',
	);
	await expect(cellOf(page, 'triage-04').getByTestId('sampling-reason')).toHaveText(
		'Least sure · doubt 0.40',
	);
	// The detail is the chip's accessible name, and its tooltip.
	const chip = cellOf(page, 'triage-03').getByTestId('sampling-reason');
	await expect(chip).toHaveAccessibleName(
		/^Least sure · doubt 0\.62\. Doubt 0\.62: stand-in-tagger pages\.confidence at version 3, asked by /,
	);
	await chip.hover();
	await expect(page.getByTestId('sampling-detail')).toContainText(
		'Doubt 0.62: stand-in-tagger pages.confidence at version 3',
	);
	await expect(page.getByTestId('grid-sampling-more')).toHaveCount(0);

	await page.getByTestId('grid-order-plain').click();

	await expect(page).toHaveURL(/queue=plain/);
	await expect
		.poll(() => gridOrder(page))
		.toEqual(['triage-00', 'triage-01', 'triage-02', 'triage-03', 'triage-04']);
	await expect(cellOf(page, 'triage-00').getByTestId('sampling-reason')).toHaveText(
		'Least sure · doubt 0.21',
	);
	// A visit scores once: switching the order asked no model again.
	expect(await samplingAsks(page, who)).toHaveLength(1);

	// The Items tab serves the same order when its address names no sort.
	await page.goto('/projects/triage?tab=items');
	await expect(page.getByRole('row').nth(1).locator('[title^="letter-001/1 ·"]')).toHaveCount(1);
	await expect(page.getByRole('row').nth(1).getByTestId('sampling-reason')).toHaveText(
		'Least sure · doubt 0.83',
	);
});

test('a project with no model endpoint and no embeddings has no sampling, and says why', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	await faultOn(page, who, '/api/assist/models', 'no-endpoint');
	await page.goto('/projects/unscored?tab=label');

	await expect(page.getByTestId('grid-sampling-line')).toHaveText(
		'Sampling cannot order this list: this deployment names no model endpoint, and no item is measured against the labelled items yet',
	);
	await expect(page.getByTestId('grid-sampling-note')).toHaveText([
		'lake~acme$maps declares no embeddings, so diversity is unavailable for its items',
		'this deployment names no model endpoint, so 2 item(s) of lake~acme$maps with no score by stand-in-tagger in their dataset stay unscored',
	]);
	await expect(page.getByTestId('grid-sampling-unasked')).toHaveText(
		'No model can be asked about the 2 items their dataset does not score.',
	);
	await expect(grid(page).getByTestId('sampling-reason')).toHaveText(
		Array(2).fill('Not scored yet'),
	);
	// Nothing a further round could reach: no Score more, and no model asked.
	await expect(page.getByTestId('grid-sampling-more')).toHaveCount(0);
	expect(await samplingAsks(page, who)).toEqual([]);
});

test('with least alike on, the items least like the labelled ones come first with their distance, are measured again on asking, and a project with nothing labelled says so', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	await page.goto('/projects/blended?tab=label');

	await expect(page.getByTestId('grid-sampling-line')).toHaveText(
		'Least alike: 3 measured, 0 not yet.',
	);
	await expect.poll(() => gridOrder(page)).toEqual(['blended-03', 'blended-02', 'blended-01']);
	await expect(grid(page).getByTestId('sampling-reason')).toHaveText([
		'Least alike · 2.00',
		'Least alike · 1.00',
		'Least alike · 0.20',
	]);
	await expect(cellOf(page, 'blended-02').getByTestId('sampling-reason')).toHaveAccessibleName(
		/from the nearest of 1 labelled item, by pages\.embedding \(stand-in-embedder\)/,
	);
	const measured = page.waitForRequest((req) =>
		req.url().endsWith('/projects/blended/sampling/reads'),
	);

	await page.getByTestId('grid-sampling-measure').click();

	expect(((await measured).postDataJSON() as { task_ids: string[] }).task_ids).toEqual([
		'blended-03',
		'blended-02',
		'blended-01',
	]);

	await page.goto('/projects/fresh?tab=label');
	await expect(page.getByTestId('grid-sampling-line')).toHaveText(
		'Sampling cannot order this list: no item is measured against the labelled items yet',
	);
	await expect(page.getByTestId('grid-sampling-note')).toHaveText(
		'no item of lake~acme$letters is labelled yet, so there is nothing to measure its items against',
	);
	await expect(page.getByTestId('grid-sampling-more')).toHaveCount(0);
});

test('the send dialog proposes N items by the strategy, the person unticks one, and the send carries the proposal and the items left', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	const dialog = await sendDialog(page);
	await expect(dialog.getByTestId('send-propose')).toHaveCount(0);
	await dialog.getByTestId('send-project').selectOption('triage');
	await dialog.getByTestId('send-propose-count').fill('2');

	await dialog.getByRole('button', { name: 'Propose 2 by sampling' }).click();

	await expect(dialog.getByTestId('send-proposal-summary')).toHaveText(
		'Sampling proposes 2 of 3 items',
	);
	// Each proposed item by its name, as the search's rows are named, its key the tooltip.
	const proposed = dialog.getByTestId('send-proposed');
	await expect(proposed).toHaveText([
		/^\s*A letter from the harbour · part 2\s+Least sure · doubt 0\.83$/,
		/^\s*A map of the harbour · part 1\s+Least sure · doubt 0\.62$/,
	]);
	await expect(proposed.first().getByTitle('letter-001/1')).toBeVisible();
	// The person unticks one, and asks for the items sampling did not choose too: the unticked one is still not sent.
	await dialog.getByRole('checkbox', { name: 'Send A map of the harbour · part 1' }).click();
	await dialog.getByTestId('send-every-candidate').getByRole('switch').click();
	const posted = page.waitForRequest((req) => req.url().endsWith('/projects/triage/items'));

	await dialog.getByRole('button', { name: 'Send 2 items', exact: true }).click();

	const sent = (await posted).postDataJSON() as {
		items: { source: { keys: string[] } }[];
		proposal: string;
	};
	expect(sent.items.map((item) => item.source.keys[0]).sort()).toEqual([
		'letter-001/0',
		'letter-001/1',
	]);
	expect(sent.proposal).toMatch(/^proposal-/);
	await expect(dialog.getByTestId('send-done')).toHaveText(/Sent 2 items\./);
});

test('a manager chooses the sampling strategy and a model that proposes, and both are saved', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Manager');
	await page.goto('/projects/letters?tab=settings');
	const strategy = page.getByLabel('Sampling', { exact: true });
	const model = page.getByLabel('Sampling model', { exact: true });
	await strategy.click();
	await page.getByRole('option', { name: 'Least sure', exact: true }).click();
	await expect(page.getByTestId('settings-sampling-said')).toHaveText(
		'The items the sampling model is least sure about come first.',
	);
	// The models that propose, and no judge alone.
	await model.click();
	await expect(page.getByRole('option')).toHaveText([
		'No model',
		'Stand-in detector (stand-in-detector)',
		'Stand-in segmenter (stand-in-segmenter)',
		'Stand-in tagger (stand-in-tagger)',
		'Stand-in that never answers (stand-in-slow)',
		'Stand-in that fails (stand-in-failing)',
	]);
	await page.getByRole('option', { name: 'Stand-in tagger (stand-in-tagger)' }).click();
	const patched = page.waitForRequest(
		(req) => req.method() === 'PATCH' && req.url().endsWith('/api/annotator/projects/letters'),
	);

	await page.getByTestId('settings-save').click();

	expect((await patched).postDataJSON()).toEqual({
		sampling: 'least_sure',
		sampling_model: 'stand-in-tagger',
	});
	await expect(page.getByTestId('settings-notice')).toHaveText('Saved.');

	// With no model endpoint, what least sure can still order is said beside it.
	await faultOn(page, who, '/api/assist/models', 'no-endpoint');
	await page.reload();
	await expect(page.getByTestId('settings-sampling-reason')).toHaveText(
		'Assist is unavailable: this deployment names no model endpoint, so least sure orders only the items their dataset scores.',
	);
});

const CHALLENGE = {
	'www-authenticate':
		'Bearer, DPoP error="invalid_token", error_description="the session must be renewed", algs="ES256"',
};

test('only a service’s own refusal of the session asks to sign in again, and a later answer from that door takes the prompt away', async ({
	page,
	context,
}) => {
	await as(context, 'Annotator');
	let answer: 'as-is' | 'forbidden' | 'relayed' | 'challenged' = 'forbidden';
	await page.route('**/api/viewer/api/**', (route) => {
		if (answer === 'as-is') return route.fallback();
		if (answer === 'forbidden')
			return route.fulfill({ status: 403, json: { detail: 'not yours' } });
		// A lakehouse refusing the person's read, relayed: a 401 with no challenge of the viewer's own.
		if (answer === 'relayed')
			return route.fulfill({
				status: 401,
				json: { detail: 'the catalog did not accept the credential' },
			});
		return route.fulfill({
			status: 401,
			json: { detail: 'Invalid or expired token' },
			headers: CHALLENGE,
		});
	});
	await page.goto('/projects/sorting?tab=label');
	await expect(grid(page)).toBeVisible();
	answer = 'relayed';
	await page.getByTestId('tab-items').click();
	await expect(page.getByRole('row').nth(1)).toBeVisible();
	await page.getByTestId('tab-label').click();
	await expect(grid(page)).toBeVisible();
	await expect(page.getByTestId('session-prompt')).toHaveCount(0);

	answer = 'challenged';
	await page.reload();
	await expect(page.getByTestId('session-prompt')).toContainText('refused by the viewer');
	answer = 'as-is';
	// A read settled within a second is reused (`reads.svelte.ts`, FRESH_MS); past it, coming back to the tab reads again.
	await page.waitForTimeout(1100);
	await page.getByTestId('tab-items').click();
	await expect(page.getByRole('row').nth(1)).toBeVisible();
	await page.getByTestId('tab-label').click();

	await expect(page.getByTestId('session-prompt')).toHaveCount(0);
});

test('an issuer that restarted with a new key asks once to sign in again on the first refused read, keeps the page behind the prompt, and the page read with the new key is let in by a service that could not read the key yet, with no second prompt', async ({
	page,
	context,
}) => {
	const who = await as(context, 'Annotator');
	const sub = encodeURIComponent(who.sub);
	const address = '/projects/sorting?tab=label&at=sorting-03#work';
	await page.goto(address);
	await expect(cellOf(page, 'sorting-03')).toHaveAttribute('tabindex', '0');
	const heading = page.getByRole('heading', { level: 1 });
	const title = await heading.textContent();
	const before = (await callsOf(page, who)).length;

	// The issuer restarts with a new key and none of its old ones; the annotator's refresher reads it at once, the
	// viewer's not yet, so the viewer still takes the session's token. Refresh reads the page again whatever it read a
	// moment ago (`refresh`, never `touch`, in `reads.svelte.ts`). The annotator's first read of it asks the issuer once
	// more: the second read without the key retires it there, so that read and every one of the page's reads that joined
	// or followed it is refused, and none is told to come back.
	await page.request.post(`${EDGE}/oidc/__rotate?sub=${sub}&kid=after-restart`);
	await page.request.post(`${EDGE}/api/viewer/__reread?sub=${sub}&service=annotator`);
	await page.getByRole('button', { name: 'Refresh' }).click();
	const prompt = page.getByTestId('session-prompt');
	await expect(prompt).toContainText('refused by the annotator');
	await expect(page.getByRole('alert').filter({ hasText: 'Sign in again' })).toHaveCount(1);
	// The prompt came on the first refused read: the annotator asked its issuer once and told nothing to come back, so
	// no read was sent again before it.
	const sinceRotation = (await callsOf(page, who))
		.slice(before)
		.filter((call) => call.service === 'annotator');
	const annotatorKeys = (
		(await (await page.request.get(`${EDGE}/api/viewer/__keysets?sub=${sub}`)).json()) as Record<
			string,
			{ forced: number }
		>
	).annotator;
	expect([
		sinceRotation.some((call) => call.status === 401),
		sinceRotation.filter((call) => call.status === 503),
		annotatorKeys?.forced,
	]).toEqual([true, [], 1]);
	// Behind the prompt the page keeps what it showed: the project's name and its grid, not an error.
	await expect(heading).toHaveText(title ?? '');
	await expect(cellOf(page, 'sorting-03')).toBeVisible();
	const asked = page.url();
	expect(new URL(asked).hash).toBe('#work');
	// The issuer is still coming up: the viewer's forced read of the new key fails once.
	await page.request.post(`${EDGE}/oidc/__keys-fail?sub=${sub}&times=1`);
	const issuer = page.waitForRequest((req) => new URL(req.url()).pathname === '/oidc/auth');
	await prompt.getByRole('button', { name: 'Sign in again' }).click();
	await issuer;

	await expect(grid(page)).toBeVisible();
	await expect(page.getByTestId('grid-cell').first()).toBeVisible();
	expect(page.url()).toBe(asked);
	// The page's parallel reads (its atlas and its descriptor), signed by the new key, met the viewer together: one
	// forced read of the issuer, which failed, so each was told to come back (503) rather than refused; each was sent
	// again and let in once the viewer had read the key, and nothing asked the person to sign in once more. The one
	// refusal the restart owes is the annotator's, of the key the issuer no longer publishes.
	const settled = async (): Promise<[number | undefined, boolean, string[], Call[]]> => {
		const keySets = (await (
			await page.request.get(`${EDGE}/api/viewer/__keysets?sub=${sub}`)
		).json()) as Record<string, { forced: number; waited: number; unavailable: number }>;
		const calls = await callsOf(page, who);
		const comeBack = calls.filter((call) => call.status === 503);
		const unanswered = comeBack
			.filter(
				(told) =>
					!calls
						.slice(calls.indexOf(told))
						.some(
							(later) =>
								later.status === 200 && later.service === told.service && later.path === told.path,
						),
			)
			.map((told) => `${told.service} ${told.path}`);
		const otherRefusals = calls.filter(
			(call) =>
				call.status === 401 &&
				!(call.service === 'annotator' && call.refused?.endsWith(`(${FIRST_KEY})`)),
		);
		return [
			keySets.viewer?.forced,
			(keySets.viewer?.waited ?? 0) > 1 && comeBack.length > 1,
			unanswered,
			otherRefusals,
		];
	};
	await expect.poll(settled, { timeout: 15_000 }).toEqual([1, true, [], []]);
	await expect(page.getByTestId('session-prompt')).toHaveCount(0);
});

for (const trigger of ['a request', 'the tab coming back'] as const)
	test(`a session past its expiry that was not renewed asks to sign in again, on ${trigger}`, async ({
		page,
		context,
	}) => {
		await as(context, 'Annotator');
		await page.clock.install();
		await page.clock.resume();
		// The tab's return is read on a page that reads nothing again on its own when the tab comes back.
		await page.goto(trigger === 'a request' ? '/projects/sorting?tab=label' : '/datasets');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		await expect(page.getByTestId('session-prompt')).toHaveCount(0);

		// A tab the browser held in the background: its clock moved on, its expiry timer did not run.
		await page.clock.setSystemTime(Date.now() + 2 * 3600_000);
		if (trigger === 'a request') await page.getByTestId('tab-items').click();
		else await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));

		await expect(page.getByTestId('session-prompt')).toBeVisible();
	});
