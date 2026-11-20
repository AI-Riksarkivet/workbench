<script lang="ts">
	// A4 · Publish. Three honest surfaces in one card:
	//  1. the CONFIRM step states what lands and whose names travel BEFORE anything runs —
	//     accepted tasks contribute shapes, skipped tasks contribute one sentinel row each, and
	//     `annotated_by` / `reviewed_by` are written into every row (publish.py's contract);
	//  2. a RUNNING publish shows the saga's actual step (`publish_progress`, narrated by the saga
	//     itself), never just a spinner;
	//  3. `publish_failed` shows the recorded error AND the step it died at, with a retry that
	//     reuses the same publish token (the saga's idempotency contract).
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import { Card } from '@riksarkivet/ui/card';
	import { Dialog } from '@riksarkivet/ui/dialog';
	import { Input } from '@riksarkivet/ui/input';
	import { Rocket, RotateCcw } from '@lucide/svelte';

	import { fireProjectEvent } from '#lib/api/projects.api.ts';
	import { personName } from './people.ts';
	import type { Contributor, People, Project, TaskListing } from './types.ts';

	let {
		project,
		listing,
		contributors = [],
		people = {},
		canPublish,
		mayPublish = true,
		onchanged,
	}: {
		project: Project;
		listing: TaskListing | null;
		/** Each person's part over every item, and their names: whose names travel with the published rows. */
		contributors?: readonly Contributor[];
		people?: People;
		/** Whether `publish` is a legal event RIGHT NOW (from the backend's legal_events). */
		canPublish: boolean;
		/** Whether the READER holds `can_publish` on the project (the manager's role). */
		mayPublish?: boolean;
		onchanged: () => void;
	} = $props();

	let confirmOpen = $state(false);
	// Empty sends no target, and the service publishes into its default namespace of the project's own warehouse; a
	// named one must be a namespace of that warehouse, which the service holds it to.
	let namespace = $state('');
	let firing = $state(false);
	let error = $state('');

	const accepted = $derived(listing?.counts['accepted'] ?? 0);
	const skipped = $derived(listing?.counts['skipped'] ?? 0);
	const annotators = $derived(
		contributors.filter((c) => c.submitted > 0).map((c) => personName(c.principal, people)),
	);
	const reviewers = $derived(
		contributors.filter((c) => c.reviewed > 0).map((c) => personName(c.principal, people)),
	);

	async function publish(): Promise<void> {
		if (firing) return;
		firing = true;
		error = '';
		const result = await fireProjectEvent({
			projectId: project.project_id,
			event: 'publish',
			targetNamespace: namespace.trim() || undefined,
		});
		firing = false;
		if (result.ok) {
			confirmOpen = false;
			onchanged();
		} else {
			error = result.detail;
		}
	}
</script>

<Card class="flex flex-col gap-3 p-4">
	<div>
		<h2 class="text-sm font-semibold">Publish to the lakehouse</h2>
		<p class="text-muted-foreground text-xs">
			One governed table per publish, created through the catalog — nothing lands before every item
			is terminal.
		</p>
	</div>
	<div class="flex flex-col gap-3 text-sm">
		{#if project.state === 'published' && project.published}
			<div class="flex flex-col gap-1">
				<p>
					<Badge variant="success">published</Badge>
					<span class="ml-1 font-mono text-xs">{project.published.table_id}</span>
				</p>
				<p class="text-muted-foreground text-xs">
					version {project.published.version} · tag
					<span class="font-mono">{project.published.tag}</span> · by {project.published
						.published_by}
				</p>
			</div>
		{:else if project.state === 'publishing'}
			<div class="flex items-center gap-2">
				<Badge variant="warning">publishing</Badge>
				<!-- The saga narrates its step onto the project document; this is that narration, not a
				     spinner. The page's poll keeps it current. -->
				<span class="text-muted-foreground text-xs">{project.publish_progress ?? 'starting…'}</span>
			</div>
		{:else if project.state === 'publish_failed'}
			<div class="flex flex-col gap-2">
				<p>
					<Badge variant="destructive">publish failed</Badge>
					{#if project.publish_progress}
						<span class="text-muted-foreground ml-1 text-xs">at: {project.publish_progress}</span>
					{/if}
				</p>
				<p class="text-destructive text-xs">{project.publish_error}</p>
				{#if project.pending_target_namespace}
					<p class="text-muted-foreground text-xs">
						The retry reuses the same publish token and must target
						<span class="font-mono">{project.pending_target_namespace}</span>.
					</p>
				{/if}
				<Button
					size="sm"
					class="w-fit"
					disabled={firing || !mayPublish}
					onclick={() => {
						namespace = project.pending_target_namespace ?? '';
						void publish();
					}}
				>
					<RotateCcw class="size-3.5" /> Retry publish
				</Button>
				{#if error}<p class="text-destructive text-xs">{error}</p>{/if}
			</div>
		{:else}
			<p class="text-muted-foreground text-xs">
				{listing?.terminal ?? 0}/{listing?.total ?? 0} items terminal —
				{#if listing?.may_publish && (listing?.total ?? 0) > 0}
					ready to publish.
				{:else}
					every item must be accepted or skipped first.
				{/if}
			</p>
			<Button
				size="sm"
				class="w-fit"
				disabled={!canPublish || !mayPublish}
				aria-describedby={canPublish && mayPublish ? undefined : 'publish-reason'}
				onclick={() => (confirmOpen = true)}
			>
				<Rocket class="size-3.5" /> Publish…
			</Button>
			{#if !mayPublish}
				<p class="text-muted-foreground text-xs" id="publish-reason">
					Only a manager of the project publishes it.
				</p>
			{:else if !canPublish}
				<p class="text-muted-foreground text-xs" id="publish-reason">
					Freeze the project first: it is published only from frozen.
				</p>
			{/if}
		{/if}
	</div>
</Card>

<Dialog.Root bind:open={confirmOpen}>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Title>Publish {project.title || project.slug}</Dialog.Title>
		<Dialog.Description>This is what lands, and whose names travel with it.</Dialog.Description>
		<div class="flex flex-col gap-2 text-sm">
			<p>
				<span class="font-medium">{accepted}</span> accepted item{accepted === 1 ? '' : 's'} contribute
				their shapes; <span class="font-medium">{skipped}</span> skipped item{skipped === 1
					? ''
					: 's'}
				land as sentinel rows (the decisions stay on the record).
			</p>
			<p class="text-muted-foreground text-xs">
				Every row carries <span class="font-mono">annotated_by</span> and
				<span class="font-mono">reviewed_by</span> — annotated by
				{annotators.length > 0 ? annotators.join(', ') : 'nobody recorded'}; reviewed by {reviewers.length >
				0
					? reviewers.join(', ')
					: 'nobody (review waived or pending)'}.
			</p>
			<label class="flex flex-col gap-1 text-sm">
				<span>Target namespace</span>
				<Input
					bind:value={namespace}
					class="font-mono"
					placeholder="the default namespace of the project's warehouse"
				/>
				<span class="text-muted-foreground text-xs">
					Empty publishes into the project's default namespace; a named one is a namespace of the
					project's own warehouse, written as warehouse$namespace. Crossing two doors: publish
					rights here, table-create rights there.
				</span>
			</label>
			{#if error}
				<p class="text-destructive text-sm">{error}</p>
			{/if}
		</div>
		<div class="flex justify-end gap-2">
			<Button variant="outline" onclick={() => (confirmOpen = false)}>Cancel</Button>
			<Button disabled={firing} onclick={() => void publish()}>
				{firing ? 'Publishing…' : namespace.trim() ? `Publish to ${namespace.trim()}` : 'Publish'}
			</Button>
		</div>
	</Dialog.Content>
</Dialog.Root>
