<script lang="ts">
	// A project's settings, each with when it may still change said beside it (the annotator's rule,
	// `AnnotatorStore.update_settings`; the door decides): the title, description and instructions; whether review is
	// required and how long a claim lasts; the consensus; the judge, a model of the deployment's endpoint that judges
	// labels (ADR 0026). Saved together, only what changed is sent.
	import { untrack } from 'svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import { Select } from '@riksarkivet/ui/select';
	import { Switch } from '@riksarkivet/ui/switch';
	import { Textarea } from '@riksarkivet/ui/textarea';

	import { judges, listModels } from '#lib/api/assist.api.ts';
	import { updateProjectSettings, type ProjectSettings } from '#lib/api/projects.api.ts';
	import { sentence } from './presentation.ts';
	import type { Project } from './types.ts';

	let {
		project,
		mayManage,
		holdsItems,
		onsaved,
	}: {
		project: Project;
		mayManage: boolean;
		/** Whether any item was sent: the consensus is fixed from then on. */
		holdsItems: boolean;
		onsaved: () => void;
	} = $props();

	// The form starts from the project as it was read; a save that lands reads the project again.
	const initial = untrack(() => ({
		title: project.title ?? '',
		description: project.description ?? '',
		instructions: project.instructions ?? '',
		review_required: project.review_required ?? true,
		lease_minutes: Math.round((project.lease_seconds ?? 1800) / 60),
		consensus_n: project.consensus_n ?? 1,
		judge_model: project.judge_model ?? '',
	}));
	let title = $state(initial.title);
	let description = $state(initial.description);
	let instructions = $state(initial.instructions);
	let reviewRequired = $state(initial.review_required);
	let leaseMinutes = $state(initial.lease_minutes);
	let consensus = $state(initial.consensus_n);
	let judgeModel = $state(initial.judge_model);
	let busy = $state(false);
	let notice = $state<{ ok: boolean; text: string } | null>(null);

	const takesWork = $derived(project.state === 'draft' || project.state === 'labeling');
	const publishing = $derived(
		project.state === 'publishing' || project.state === 'published' || project.state === 'archived',
	);
	const editable = $derived(mayManage && !publishing);

	// THE JUDGES the deployment's endpoint serves (models that judge labels), read for a manager. A judge the project
	// names that the endpoint does not list now stays offered, so it can be seen and cleared.
	const modelsRead = $derived(mayManage ? listModels({ project: project.project_id }) : null);
	const models = $derived(modelsRead?.current?.ok ? modelsRead.current.data : null);
	const modelsFailure = $derived(
		modelsRead?.current && !modelsRead.current.ok ? modelsRead.current.detail : null,
	);
	const judgeOptions = $derived.by(() => {
		const offered = (models?.models ?? [])
			.filter(judges)
			.map((m) => ({ value: m.name, label: m.title ? `${m.title} (${m.name})` : m.name }));
		const named = project.judge_model ?? '';
		return named && !offered.some((o) => o.value === named)
			? [...offered, { value: named, label: `${named} (not served by the model endpoint now)` }]
			: offered;
	});

	const changes = $derived.by<ProjectSettings>(() => {
		const out: ProjectSettings = {};
		if (title !== (project.title ?? '')) out.title = title;
		if (description !== (project.description ?? '')) out.description = description;
		if (instructions !== (project.instructions ?? '')) out.instructions = instructions;
		if (reviewRequired !== (project.review_required ?? true)) out.review_required = reviewRequired;
		if (leaseMinutes * 60 !== (project.lease_seconds ?? 1800))
			out.lease_seconds = leaseMinutes * 60;
		if (consensus !== (project.consensus_n ?? 1)) out.consensus_n = consensus;
		if (judgeModel !== (project.judge_model ?? '')) out.judge_model = judgeModel;
		return out;
	});
	const dirty = $derived(Object.keys(changes).length > 0);

	async function save(): Promise<void> {
		if (busy || !dirty) return;
		busy = true;
		notice = null;
		const result = await updateProjectSettings({
			projectId: project.project_id,
			settings: changes,
		});
		busy = false;
		notice = result.ok ? { ok: true, text: 'Saved.' } : { ok: false, text: result.detail };
		if (result.ok) onsaved();
	}
</script>

<form
	class="flex flex-col gap-3"
	data-testid="settings-form"
	onsubmit={(e) => {
		e.preventDefault();
		void save();
	}}
>
	<h2 class="text-muted-foreground text-xs font-medium uppercase">Project</h2>
	{#if publishing}
		<p class="text-muted-foreground text-xs">
			This project's settings are fixed: a publish has started.
		</p>
	{:else if !mayManage}
		<p class="text-muted-foreground text-xs">Its managers change a project's settings.</p>
	{/if}
	<label class="flex flex-col gap-1 text-sm">
		Title
		<Input bind:value={title} maxlength={200} disabled={!editable} data-testid="settings-title" />
	</label>
	<label class="flex flex-col gap-1 text-sm">
		Description <span class="text-muted-foreground text-xs">What is labeled, and why.</span>
		<Textarea bind:value={description} rows={2} maxlength={4000} disabled={!editable} />
	</label>
	<label class="flex flex-col gap-1 text-sm">
		Instructions <span class="text-muted-foreground text-xs"
			>How to label: shown to everyone who labels.</span
		>
		<Textarea
			bind:value={instructions}
			rows={4}
			maxlength={20000}
			disabled={!editable}
			data-testid="settings-instructions"
		/>
	</label>
	<p class="text-muted-foreground text-xs">
		Title, description and instructions change until a publish starts.
	</p>

	<div class="flex flex-col gap-2 border-t pt-3">
		<label class="flex items-center gap-2 text-sm" data-testid="settings-review">
			<Switch bind:checked={reviewRequired} disabled={!editable || !takesWork} />
			Every submission is reviewed before it is accepted
		</label>
		<label class="flex items-center gap-2 text-sm">
			A claim lasts
			<Input
				type="number"
				min={1}
				max={10080}
				bind:value={leaseMinutes}
				class="h-8 w-24"
				disabled={!editable || !takesWork}
			/>
			minutes
		</label>
		<p class="text-muted-foreground text-xs">
			These change while the project takes work (draft or labeling). Items nobody has started take
			the new values; an item someone has claimed keeps the ones it was sent with.
		</p>
	</div>

	<div class="flex flex-col gap-2 border-t pt-3">
		<label class="flex items-center gap-2 text-sm">
			Each item is labeled independently by
			<Input
				type="number"
				min={1}
				max={5}
				bind:value={consensus}
				class="h-8 w-20"
				disabled={!editable || holdsItems}
				data-testid="settings-consensus"
			/>
			{consensus === 1 ? 'person' : 'people'}
		</label>
		<p class="text-muted-foreground text-xs">
			{holdsItems
				? 'Fixed: items were sent, each made into this many replicas.'
				: 'Changes until the first items are sent.'}
		</p>
	</div>

	<div class="flex flex-col gap-2 border-t pt-3">
		<div class="flex flex-col gap-1 text-sm" data-testid="settings-judge">
			Judge
			<span class="text-muted-foreground text-xs">
				A model that reads each submitted item and flags the labels it disagrees with. It decides
				nothing: reviewers still accept or send back every item, the flagged ones first.
			</span>
			<Select
				bind:value={judgeModel}
				ariaLabel="Judge"
				disabled={!editable}
				options={[{ value: '', label: 'No judge' }, ...judgeOptions]}
			/>
		</div>
		{#if modelsFailure !== null}
			<p class="text-destructive text-xs" data-testid="settings-judge-reason">
				The model endpoint's judges could not be listed: {modelsFailure}
			</p>
		{:else if models !== null && !models.available}
			<p class="text-muted-foreground text-xs" data-testid="settings-judge-reason">
				{sentence(models.reason)}.
			</p>
		{:else if models !== null && judgeOptions.length === 0}
			<p class="text-muted-foreground text-xs" data-testid="settings-judge-reason">
				The model endpoint serves no model that judges labels.
			</p>
		{/if}
	</div>

	{#if editable}
		<div class="flex items-center gap-3">
			<Button type="submit" size="sm" disabled={busy || !dirty} data-testid="settings-save">
				{busy ? 'Saving…' : 'Save the settings'}
			</Button>
			{#if notice}
				<span
					class={notice.ok ? 'text-success text-sm' : 'text-destructive text-sm'}
					role="status"
					data-testid="settings-notice">{notice.text}</span
				>
			{/if}
		</div>
	{/if}
</form>
