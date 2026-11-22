<script lang="ts">
	/**
	 * Send a selection to a project: the rows a dataset's search answered, or the rows a region of its atlas selected.
	 * Each becomes an item of the project, appended to one still taking items (draft or labeling) or to a new project
	 * created around the selection, optionally labelled as one class of the project's labels. A new project is made with
	 * its classes and their shape kind, as the New project form makes one: an item is labelled with its project's
	 * classes, so a project without any would leave every item it is sent unclassable. The project's own door decides:
	 * `can_send_items` on it, and the dataset must be one the reader can read.
	 *
	 * SAMPLING (ADR 0034): where the chosen project's strategy is on, sampling proposes which of the selection to send,
	 * each with why; the person unticks what they would not send, or sends every candidate after all. A send while a
	 * proposal is shown names it, so the annotator records which items sampling chose and which the person added.
	 */
	import { resolve } from '$app/paths';
	import { SvelteSet } from 'svelte/reactivity';
	import { Button } from '@riksarkivet/ui/button';
	import { Checkbox } from '@riksarkivet/ui/checkbox';
	import { Dialog } from '@riksarkivet/ui/dialog';
	import { Input } from '@riksarkivet/ui/input';
	import { Select } from '@riksarkivet/ui/select';
	import { Switch } from '@riksarkivet/ui/switch';
	import { createProject, fetchProject, listProjects, sendItems } from '#lib/api/projects.api.ts';
	import { PROPOSAL_CAP, proposeItems } from '#lib/api/sampling.api.ts';
	import { reasonText } from './sampling.ts';
	import { keysToSend, sendItemsFor, sendRefusal, type SendSource } from './send.ts';
	import type { Proposal } from './types.ts';

	let {
		open = $bindable(false),
		keys,
		total,
		from,
		origin,
		orgs,
		project = null,
		names = new Map(),
	}: {
		open?: boolean;
		/** The selection's key paths (`doc/…`), as the dataset's descriptor joins them. */
		keys: string[];
		/** How many rows the selection holds, listed or not: more than `keys` is a partial selection, refused. */
		total: number;
		from: SendSource;
		/** Which surface the selection is from, said in the title. */
		origin: 'search' | 'atlas' | 'rows';
		/** The organisations the reader belongs to: whose projects are offered, and where a new one is made. */
		orgs: string[];
		/** The project the page works in (`?project=`), offered first. */
		project?: string | null;
		/** What each candidate is called, by its key (`namesByKey`); a key with no name is shown as itself. */
		names?: ReadonlyMap<string, string>;
	} = $props();

	// Only the projects the reader may send items to, as the store answers it (`?can=can_send_items`).
	const reads = $derived(
		open
			? orgs.map((org) => ({ org, read: listProjects({ tenant: org, can: 'can_send_items' }) }))
			: [],
	);
	/** Of those, only projects still taking items: a send is legal in draft and labeling alone. */
	const taking = $derived(
		reads.flatMap(({ org, read }) =>
			read.current?.ok
				? read.current.data.projects
						.filter((p) => p.state === 'draft' || p.state === 'labeling')
						.map((p) => ({ id: p.project_id, name: p.title || p.slug, org }))
				: [],
		),
	);
	const failed = $derived(
		reads.flatMap(({ org, read }) =>
			read.current && !read.current.ok ? [`${org}: ${read.current.detail}`] : [],
		),
	);
	const loading = $derived(reads.some(({ read }) => read.current === undefined));

	/** The page's project when the reader may send to it; otherwise the reader chooses. */
	let chosen = $derived<string>(taking.some((p) => p.id === project) ? (project ?? '') : '');
	const chosenRead = $derived(chosen ? fetchProject({ projectId: chosen }) : null);
	/** The chosen project's labels, to send the selection as one of them. */
	const labels = $derived(
		chosenRead?.current?.ok
			? chosenRead.current.data.project.ontology.classes.map((c) => c.name)
			: [],
	);
	/** The label to send as, one of the chosen project's: a new choice of project starts with none. */
	let label = $derived.by((): string => {
		void chosen;
		return '';
	});
	/** The chosen project's sampling: whether it proposes what to send. */
	const samples = $derived(
		chosenRead?.current?.ok ? chosenRead.current.data.project.sampling !== 'off' : false,
	);
	/** How many sampling is asked to propose: 50, or every candidate when fewer. */
	let proposeCount = $derived(Math.min(50, keys.length, PROPOSAL_CAP));
	/** What sampling proposed for the chosen project; a new choice of project starts with none. */
	let proposal = $derived.by((): Proposal | null => {
		void chosen;
		return null;
	});
	/** The proposed keys the person leaves ticked. */
	const ticked = new SvelteSet<string>();
	/** Whether the send carries every candidate after all, beside what sampling chose. */
	let everyCandidate = $state(false);
	let proposing = $state(false);
	const sendKeys = $derived(
		keysToSend(
			keys,
			proposal
				? { chosen: new Set(proposal.chosen.map((c) => c.key)), ticked, everyCandidate }
				: null,
		),
	);

	async function propose(): Promise<void> {
		if (!chosen || proposing) return;
		const count = Math.trunc(Number(proposeCount));
		if (!(count >= 1 && count <= PROPOSAL_CAP)) {
			error = `Propose between 1 and ${Math.min(keys.length, PROPOSAL_CAP)} items.`;
			return;
		}
		proposing = true;
		error = '';
		const result = await proposeItems(chosen, { dataset: from.dataset, keys, count });
		proposing = false;
		if (!result.ok) {
			error = result.detail;
			return;
		}
		ticked.clear();
		for (const c of result.data.chosen) ticked.add(c.key);
		everyCandidate = false;
		proposal = result.data;
	}

	function discardProposal(): void {
		proposal = null;
		ticked.clear();
		everyCandidate = false;
	}

	let newSlug = $state('');
	let newTitle = $state('');
	let newClasses = $state('');
	let newShape = $state('bbox');
	/** The shape kinds a class may be drawn with, as the New project form offers them. */
	const SHAPE_KINDS = ['bbox', 'polygon', 'mask', 'segment', 'tag', 'text'];
	const newClassNames = $derived([
		...new Set(
			newClasses
				.split(',')
				.map((c) => c.trim())
				.filter(Boolean),
		),
	]);
	let newOrg = $derived(orgs[0] ?? '');
	let sending = $state(false);
	let error = $state('');
	let done = $state<{ projectId: string; sent: number; created: number } | null>(null);

	const refusal = $derived(sendRefusal({ fetched: keys.length, total }));
	const count = $derived(`${total.toLocaleString()} item${total === 1 ? '' : 's'}`);
	const items = (n: number): string => `${n.toLocaleString()} item${n === 1 ? '' : 's'}`;

	async function sendTo(projectId: string): Promise<void> {
		sending = true;
		error = '';
		const proposed = proposal?.project_id === projectId ? proposal : null;
		const result = await sendItems({
			projectId,
			items: sendItemsFor(proposed ? sendKeys : keys, from, label),
			...(proposed ? { proposal: proposed.proposal_id } : {}),
		});
		sending = false;
		// The door's own words: a 403 names the missing role, a 409 a project no longer taking items.
		if (result.ok) {
			done = { projectId, sent: result.data.sent, created: result.data.created };
			discardProposal();
		} else error = result.detail;
	}

	async function createAndSend(): Promise<void> {
		const slug = newSlug.trim();
		if (!slug || !newOrg || newClassNames.length === 0 || sending) return;
		sending = true;
		error = '';
		const created = await createProject({
			tenant: newOrg,
			slug,
			title: newTitle.trim(),
			ontology: {
				classes: newClassNames.map((name) => ({ name, tools: [newShape], required: false })),
			},
		});
		sending = false;
		if (!created.ok) {
			error = created.detail;
			return;
		}
		// The new project is now the chosen one, read back from the list, so a send that fails is retried by sending
		// alone: the project is not created twice.
		await listProjects({ tenant: newOrg, can: 'can_send_items' }).refresh();
		chosen = created.data.project_id;
		newSlug = '';
		newTitle = '';
		newClasses = '';
		await sendTo(created.data.project_id);
	}

	function reset(next: boolean): void {
		open = next;
		if (next) return;
		done = null;
		error = '';
	}

	/** What a send did, and nothing it did not. */
	function outcome(sent: number, created: number): string {
		const items = `${sent} item${sent === 1 ? '' : 's'}`;
		if (created === sent) return `Sent ${items}.`;
		const already = sent - created;
		return `Sent ${items}: ${created} new, ${already} already in the project.`;
	}

	const projectHref = (id: string): string =>
		`${resolve('/projects/[project]', { project: encodeURIComponent(id) })}?${new URLSearchParams({ project: id })}`;
</script>

<Dialog.Root bind:open={() => open, reset}>
	<Dialog.Content class="sm:max-w-md" data-testid="send-to-project">
		<Dialog.Title>Send {count} to a project</Dialog.Title>
		<Dialog.Description>
			From {origin === 'atlas'
				? 'the atlas selection'
				: origin === 'rows'
					? 'the rows listed'
					: 'the search results'} on
			<span class="font-mono break-all">{from.dataset}</span>. Each becomes an item of the project;
			where it came from travels with it.
		</Dialog.Description>

		{#if done}
			<div class="flex flex-col gap-2 text-sm" role="status" data-testid="send-done">
				<p>
					{outcome(done.sent, done.created)}
				</p>
				<!-- `projectHref` resolves the route id. -->
				<!-- eslint-disable-next-line svelte/no-navigation-without-resolve -->
				<a class="w-fit underline underline-offset-2" href={projectHref(done.projectId)}
					>Open the project</a
				>
			</div>
		{:else if refusal}
			<p class="text-sm" role="alert" data-testid="send-refused">{refusal}</p>
		{:else}
			<form
				class="flex flex-col gap-3 text-sm"
				onsubmit={(e) => {
					e.preventDefault();
					if (chosen) void sendTo(chosen);
				}}
			>
				<label class="flex flex-col gap-1">
					<span>Project</span>
					<select
						class="border-input bg-background focus-visible:ring-ring/50 h-9 rounded-lg border px-2 outline-none focus-visible:ring-3"
						bind:value={chosen}
						data-testid="send-project"
					>
						<option value="">{loading ? 'Reading the projects…' : 'Choose a project'}</option>
						{#each taking as p (p.id)}
							<option value={p.id}>{p.name} ({p.org})</option>
						{/each}
					</select>
				</label>
				{#if !loading && taking.length === 0 && failed.length === 0}
					<p class="text-muted-foreground">No project is taking items: create one below.</p>
				{/if}
				{#each failed as reason, i (`${i}:${reason}`)}
					<p class="text-destructive break-words">The projects could not be read: {reason}</p>
				{/each}
				{#if labels.length > 0}
					<label class="flex flex-col gap-1">
						<span>Label every item as</span>
						<select
							class="border-input bg-background focus-visible:ring-ring/50 h-9 rounded-lg border px-2 outline-none focus-visible:ring-3"
							bind:value={label}
						>
							<option value="">No label: send them blank</option>
							{#each labels as name (name)}
								<option value={name}>{name}</option>
							{/each}
						</select>
					</label>
				{/if}
				{#if samples && proposal === null}
					<div class="flex flex-wrap items-center gap-2" data-testid="send-propose">
						<Input
							type="number"
							min={1}
							max={Math.min(keys.length, PROPOSAL_CAP)}
							bind:value={proposeCount}
							class="h-8 w-20"
							aria-label="How many sampling proposes"
							data-testid="send-propose-count"
						/>
						<Button
							type="button"
							variant="outline"
							size="sm"
							disabled={proposing}
							data-testid="send-propose-run"
							onclick={() => void propose()}
						>
							{proposing ? 'Proposing…' : `Propose ${proposeCount} by sampling`}
						</Button>
					</div>
				{:else if proposal}
					<div class="flex flex-col gap-2" data-testid="send-proposal">
						<p data-testid="send-proposal-summary">
							Sampling proposes {proposal.chosen.length} of {items(keys.length)}
						</p>
						{#each proposal.notes as note (note)}
							<p class="text-muted-foreground text-xs" data-testid="send-proposal-note">{note}</p>
						{/each}
						{#if proposal.chosen.length > 0}
							<ul class="flex max-h-48 flex-col gap-1 overflow-y-auto">
								{#each proposal.chosen as c (c.key)}
									{@const name = names.get(c.key) ?? c.key}
									<li>
										<label
											class="flex items-center gap-2 text-xs"
											data-testid="send-proposed"
											data-key={c.key}
										>
											<Checkbox
												checked={ticked.has(c.key)}
												onCheckedChange={(on) => (on ? ticked.add(c.key) : ticked.delete(c.key))}
												aria-label={`Send ${name}`}
											/>
											<span class="min-w-0 break-words" title={c.key}>{name}</span>
											<span class="text-muted-foreground ml-auto shrink-0"
												>{reasonText(c.kind, c.score)}</span
											>
										</label>
									</li>
								{/each}
							</ul>
						{/if}
						<label class="flex items-center gap-2 text-xs" data-testid="send-every-candidate">
							<Switch bind:checked={everyCandidate} />
							Also send the items sampling did not choose
						</label>
						<Button
							type="button"
							variant="ghost"
							size="sm"
							class="w-fit"
							data-testid="send-proposal-discard"
							onclick={discardProposal}
						>
							Send all {items(keys.length)} selected instead
						</Button>
					</div>
				{/if}
				<Button
					type="submit"
					class="w-fit"
					disabled={sending || !chosen || (proposal !== null && sendKeys.length === 0)}
				>
					{sending ? 'Sending…' : `Send ${proposal !== null ? items(sendKeys.length) : count}`}
				</Button>
			</form>

			<form
				class="border-border flex flex-col gap-2 border-t pt-3 text-sm"
				onsubmit={(e) => {
					e.preventDefault();
					void createAndSend();
				}}
			>
				<p class="text-muted-foreground">Or create a project around this selection:</p>
				{#if orgs.length > 1}
					<label class="flex flex-col gap-1">
						<span>Organisation</span>
						<select
							class="border-input bg-background focus-visible:ring-ring/50 h-9 rounded-lg border px-2 outline-none focus-visible:ring-3"
							bind:value={newOrg}
						>
							{#each orgs as org (org)}
								<option value={org}>{org}</option>
							{/each}
						</select>
					</label>
				{/if}
				<div class="flex flex-col gap-2 sm:flex-row">
					<label class="flex flex-1 flex-col gap-1">
						<span>Slug</span>
						<Input bind:value={newSlug} placeholder="letters-2026" pattern="[a-z0-9\-]+" required />
					</label>
					<label class="flex flex-1 flex-col gap-1">
						<span>Title</span>
						<Input bind:value={newTitle} placeholder="Letters" />
					</label>
				</div>
				<div class="flex flex-col gap-2 sm:flex-row">
					<label class="flex flex-[2] flex-col gap-1">
						<span>Label classes <span class="text-muted-foreground">(comma-separated)</span></span>
						<Input
							bind:value={newClasses}
							placeholder="letter, map, photograph"
							required
							data-testid="send-new-classes"
						/>
					</label>
					<label class="flex flex-1 flex-col gap-1">
						<span>Shape kind</span>
						<Select
							bind:value={newShape}
							ariaLabel="Shape kind"
							options={SHAPE_KINDS.map((kind) => ({ value: kind, label: kind }))}
						/>
					</label>
				</div>
				<Button
					type="submit"
					variant="outline"
					class="w-fit"
					disabled={sending || !newSlug.trim() || !newOrg || newClassNames.length === 0}
				>
					Create and send {count}
				</Button>
			</form>
		{/if}

		{#if error}
			<p class="text-destructive text-sm break-words" role="alert" data-testid="send-error">
				{error}
			</p>
		{/if}
	</Dialog.Content>
</Dialog.Root>
