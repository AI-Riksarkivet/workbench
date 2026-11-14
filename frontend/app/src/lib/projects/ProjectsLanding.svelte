<script lang="ts">
	// The projects the reader can see, across every organisation they belong to, each with its state and progress, and
	// the designer that creates one. Items arrive by being SENT into a project, from a dataset's search or atlas.
	import { resolve } from '$app/paths';
	import { Plus } from '@lucide/svelte';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import { ANNOTATOR_HINT } from './presentation.ts';
	import { Badge } from '@workbench/ui/badge';
	import { Button } from '@workbench/ui/button';
	import { Card } from '@workbench/ui/card';
	import { Checkbox } from '@workbench/ui/checkbox';
	import { Input } from '@workbench/ui/input';
	import { Textarea } from '@workbench/ui/textarea';
	import { Progress } from '@workbench/ui/progress';
	import { Select } from '@workbench/ui/select';
	import * as Tabs from '@workbench/ui/tabs';
	import { FolderPlus, RefreshCw } from '@lucide/svelte';

	import { createProject, listProjects } from '#lib/api/projects.api.ts';
	import type { Project } from './types.ts';
	import { projectStateVariant, taskProgress } from './presentation.ts';
	import { PROJECT_TEMPLATES, templateById } from './templates.ts';
	import TaskPreview from './TaskPreview.svelte';
	import {
		DRAW_TOOLS,
		type DraftClass,
		type TaskDraft,
		draftClassFromWire,
		draftToOntology,
		draftToYaml,
		parseTaskYaml,
	} from './task-yaml.ts';

	/** The organisations the reader belongs to (`GET /me`): each one's projects are listed, and a new project is
	 *  created in one of them. */
	let { orgs }: { orgs: string[] } = $props();

	// One read per organisation: a refusal or a failure is that organisation's, and the others still list.
	const reads = $derived(orgs.map((org) => ({ org, read: listProjects({ tenant: org }) })));
	/** The organisation a new project is created in: the only one, or the one the reader picks. */
	let tenant = $derived(orgs[0] ?? '');

	let createOpen = $state(false);
	let creating = $state(false);
	let createError = $state('');
	let slug = $state('');
	let title = $state('');
	let description = $state('');
	let instructions = $state('');
	let reviewRequired = $state(true);
	// Consensus v1: how many INDEPENDENT annotators label each sent item. N>1 seeds N replica
	// items per item at send; one annotator may hold at most one replica of a group.
	let consensusN = $state(1);
	// The labeling TASK, specified at create — ONE document now (`LabelOntology`).
	//
	// The taxonomy and what is mandatory are separate properties of it: naming a class never makes it
	// required.
	let classesText = $state('');
	let shapeKind = $state('bbox');
	let classesRequired = $state(false);
	const SHAPE_KINDS = ['bbox', 'polygon', 'mask', 'segment', 'tag', 'text'];

	// PRESETS, aligned with Hugging Face pipeline ids by convention — `kind` is a free string
	// server-side, so this list is a starting vocabulary, not a closed enum. Picking one seeds the
	// tools each class gets; the enforceable contract is the classes themselves.
	let taskKind = $state('free');
	const TASK_PRESETS: Record<
		string,
		{ tools: string[]; attributes?: { name: string; type?: string; required?: boolean }[] }
	> = {
		'object-detection': { tools: ['bbox'] },
		'image-segmentation': { tools: ['polygon', 'mask'] },
		'image-classification': { tools: ['tag'] },
		'token-classification': { tools: ['text'] },
		'image-to-text': { tools: ['bbox', 'text'] },
		'document-question-answering': { tools: ['bbox', 'text'] },
		'reading-order': {
			tools: ['bbox'],
			attributes: [{ name: 'order', type: 'int', required: true }],
		},
	};

	const classNames = $derived(
		classesText
			.split(',')
			.map((c) => c.trim())
			.filter(Boolean),
	);

	// THE TEMPLATE GALLERY (finding 8). A template is a COMPLETE ontology — per-class tools,
	// typed attributes, relations — everything the free-form path below cannot author (it applies
	// one uniform tool list to every typed name). Picking one seeds an EDITABLE working copy:
	// "customize how the labeling task is supposed to look" means the template is a starting
	// point, not a contract you can only take or leave. 'custom' keeps the free-form path;
	// 'yaml' starts from a scaffold in the YAML view.
	let templateId = $state('custom');
	const pickedTemplate = $derived(templateById(templateId));

	// ONE working copy — `TaskDraft` — whichever view edits it. The FORM speaks the honest
	// vocabulary (draw / transcribe / span / tag — the wire's `tools` conflates the last three
	// and cannot say transcription at all); the YAML view is the full-power surface (typed
	// fields, relations, allow_empty) and the exchange format. Both views project the same
	// draft, so they cannot disagree.
	let draft = $state<TaskDraft>({ kind: '', classes: [], relations: [], allowEmpty: undefined });
	let editorView = $state<'form' | 'yaml'>('form');
	let yamlText = $state('');
	let yamlErrors = $state<string[]>([]);
	const STARTER_YAML = [
		'# The labeling task: what annotators mark, and how each label is captured.',
		'task: my-task',
		'labels:',
		'  - name: region',
		'    draw: [bbox] # bbox | polygon | mask | segment',
		'    transcribe: true # regions carry transcribed text',
		'  - name: person',
		'    span: true # marks ranges in the text (NER)',
		'  - name: damaged',
		'    tag: true # a whole-item choice',
		'',
	].join('\n');
	// Picking a template seeds the draft once, on the pick: editing the draft afterwards never restores the template.
	function pickTemplate(next: string): void {
		templateId = next;
		yamlErrors = [];
		if (next === 'yaml') {
			draft = parseTaskYaml(STARTER_YAML).draft;
			yamlText = STARTER_YAML;
			editorView = 'yaml';
			return;
		}
		const t = templateById(next);
		draft = {
			kind: t?.ontology.kind ?? '',
			classes: (t?.ontology.classes ?? []).map(draftClassFromWire),
			relations: (t?.ontology.relations ?? []).map((r) => ({
				name: r.name,
				from: [...r.from_classes],
				to: [...r.to_classes],
				directed: true,
				required: r.required ?? false,
			})),
			allowEmpty: t?.ontology.allow_empty,
		};
		editorView = 'form';
	}
	function showYaml(): void {
		yamlText = draftToYaml(draft);
		yamlErrors = [];
		editorView = 'yaml';
	}
	/** Every valid parse lands in the draft at once; an invalid one shows its errors and leaves
	 *  the draft on the last valid state — and BLOCKS create, so what is sent is never silently
	 *  older than what is on screen. */
	function onYamlInput(text: string): void {
		yamlText = text;
		const { draft: parsed, errors } = parseTaskYaml(text);
		yamlErrors = errors;
		if (errors.length === 0) draft = parsed;
	}
	function toggleDraw(row: DraftClass, tool: string): void {
		row.draw = row.draw.includes(tool) ? row.draw.filter((t) => t !== tool) : [...row.draw, tool];
	}
	/** The surviving relations, live — edges whose endpoints were renamed or removed are dropped
	 *  at payload time, and the editor says so instead of letting the server refuse the create. */
	const liveRelations = $derived.by(() => {
		const names = new Set(draft.classes.map((c) => c.name.trim()));
		return draft.relations.filter((r) => [...r.from, ...r.to].every((n) => names.has(n)));
	});

	// Derived, not a function: a pure projection of taskKind + classesText + the two toggles, which
	// is exactly what $derived is for. One payload — there is nothing left for a second to disagree
	// with.
	const ontologyPayload = $derived.by(() => {
		// The EDITED working copy wins: the declarations as the person left them, whichever view
		// authored them. `draftToOntology` maps the vocabulary onto the wire document, dropping
		// classes that can produce nothing and edges naming a ghost endpoint (the server would
		// refuse the whole create over either).
		if (templateId !== 'custom') {
			const doc = draftToOntology(draft);
			return doc.classes.length > 0 || doc.kind ? doc : undefined;
		}
		if (classNames.length === 0 && taskKind === 'free') return undefined;
		const preset = TASK_PRESETS[taskKind];
		// The preset's tools when one is picked, else the single shape kind. Per-class tools are
		// expressible in the model and NOT yet authorable here — a real per-class editor is its own
		// piece of work, so every class currently gets the same list.
		const tools = preset?.tools ?? [shapeKind];
		return {
			...(taskKind === 'free' ? {} : { kind: taskKind }),
			classes: classNames.map((name) => ({
				name,
				tools,
				attributes: preset?.attributes ?? [],
				required: classesRequired,
			})),
		};
	});

	function refreshAll(): void {
		for (const { read } of reads) void read.refresh();
	}

	/** A project's page, carrying it as the reader's project (`?project=`), as every in-app link does. */
	const projectHref = (id: string): string =>
		`${resolve('/projects/[project]', { project: encodeURIComponent(id) })}?${new URLSearchParams({ project: id })}`;

	async function create(): Promise<void> {
		// Unparseable YAML blocks create: the draft holds the LAST VALID state, and sending it
		// while the screen shows something newer would be a silent divergence.
		if (!tenant || !slug.trim() || creating || yamlErrors.length > 0) return;
		creating = true;
		createError = '';
		const result = await createProject({
			tenant,
			slug: slug.trim(),
			title: title.trim(),
			description: description.trim(),
			instructions: instructions.trim(),
			review_required: reviewRequired,
			consensus_n: Math.min(5, Math.max(1, Math.round(consensusN) || 1)),
			...(ontologyPayload ? { ontology: ontologyPayload } : {}),
		});
		creating = false;
		if (result.ok) {
			createOpen = false;
			slug = title = description = instructions = '';
			consensusN = 1;
		} else {
			// 403 is a permission fact, not a form mistake — say which door closed.
			createError = result.detail;
		}
	}
</script>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-4 overflow-y-auto p-4 sm:p-6">
	<header class="flex flex-wrap items-end justify-between gap-3">
		<div>
			<h1 class="text-2xl font-semibold">Projects</h1>
			<p class="text-muted-foreground text-sm">
				Send items in from a dataset's search or atlas, then claim, annotate, review and publish.
			</p>
		</div>
		<div class="flex items-center gap-2">
			<Button variant="outline" size="sm" disabled={orgs.length === 0} onclick={refreshAll}>
				<RefreshCw class="size-3.5" /> Refresh
			</Button>
			<Button
				size="sm"
				disabled={orgs.length === 0}
				aria-describedby={orgs.length === 0 ? 'projects-no-org' : undefined}
				aria-expanded={createOpen}
				onclick={() => (createOpen = !createOpen)}
			>
				<FolderPlus class="size-3.5" /> New project
			</Button>
		</div>
	</header>

	{#if createOpen}
		{@render createTaskPanel()}
	{/if}

	{#if orgs.length === 0}
		<Card class="flex flex-col items-start gap-1 px-4 py-6 text-sm" data-testid="no-org">
			<p class="font-medium">You belong to no organisation.</p>
			<p class="text-muted-foreground" id="projects-no-org">
				A project lives in an organisation, and an organisation's members see its projects. Ask an
				administrator of one to add you.
			</p>
		</Card>
	{/if}

	{#each reads as { org, read } (org)}
		<section class="flex flex-col gap-3" aria-labelledby="org-{org}">
			<h2 id="org-{org}" class="text-muted-foreground text-sm font-medium">
				<span class="sr-only">Organisation </span><span class="font-mono">{org}</span>
			</h2>
			{#if !read.current}
				<p class="text-muted-foreground text-sm" role="status">Reading the projects…</p>
			{:else if !read.current.ok}
				{#if read.current.status === 403 || read.current.status === 401}
					<Card class="px-4 py-4 text-sm" data-testid="projects-refused">
						<p class="font-medium">You may not list the projects of {org}.</p>
						<p class="text-muted-foreground mt-1 break-words">{read.current.detail}</p>
					</Card>
				{:else}
					<LoadFailure
						what="The projects of {org} could not be read."
						detail={read.current.detail}
						hint={ANNOTATOR_HINT}
						onretry={() => void read.refresh()}
					/>
				{/if}
			{:else if read.current.data.projects.length === 0}
				<Card class="flex flex-col items-start gap-2 px-4 py-6 text-sm" data-testid="no-projects">
					<p class="font-medium">No projects yet.</p>
					<p class="text-muted-foreground">
						Create one, then send items into it from a dataset's search results or its atlas.
					</p>
					<Button
						size="sm"
						class="mt-1"
						onclick={() => {
							tenant = org;
							createOpen = true;
						}}
					>
						<Plus class="size-3.5" /> New project in {org}
					</Button>
				</Card>
			{:else}
				<ul
					class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3"
					aria-label="Projects of {org}"
				>
					{#each read.current.data.projects as project (project.project_id)}
						{@const progress = taskProgress(project.counts)}
						<li>
							<!-- `projectHref` resolves the route id. -->
							<!-- eslint-disable svelte/no-navigation-without-resolve -->
							<a
								href={projectHref(project.project_id)}
								class="group focus-visible:ring-ring/50 block h-full rounded-xl outline-none focus-visible:ring-3"
							>
								<!-- eslint-enable svelte/no-navigation-without-resolve -->
								<Card
									class="group-hover:border-ring flex h-full flex-col gap-2 p-4 transition-colors"
								>
									<div class="flex items-start justify-between gap-2">
										<span class="min-w-0 text-sm font-semibold break-words"
											>{project.title || project.slug}</span
										>
										<Badge variant={projectStateVariant(project.state)}
											>{project.state.replace('_', ' ')}</Badge
										>
									</div>
									<span class="text-muted-foreground font-mono text-xs break-all"
										>{project.slug}</span
									>
									{#if project.description}
										<p class="text-muted-foreground line-clamp-2 text-xs">{project.description}</p>
									{/if}
									<Progress
										value={progress.done}
										max={Math.max(progress.total, 1)}
										aria-label="Items finished in {project.title || project.slug}"
									/>
									<p class="text-muted-foreground text-xs">
										{progress.done}/{progress.total} items finished
										{#if project.publish_error}
											· <span class="text-destructive">publish failed</span>
										{:else if project.state === 'publishing'}
											· {project.publish_progress ?? 'publishing…'}
										{/if}
									</p>
								</Card>
							</a>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	{/each}
</div>

{#snippet createTaskPanel()}
	<!-- INLINE, deliberately: a task DESIGNER (template gallery, per-label rows, a YAML pane, relations)
	     in a narrow modal puts the editor below the fold of its own popup. In the page flow it gets the page's
	     width: what the task is ABOUT on the left, the task DESIGN — the part that needs the
	     room — on the right. -->
	<Card class="flex flex-col gap-4 p-4" data-testid="create-task">
		<div>
			<h2 class="text-base font-semibold">New project</h2>
			<p class="text-muted-foreground text-sm">
				Born in <span class="font-mono">draft</span> — open it for labeling once items are sent.
			</p>
		</div>
		<form
			class="grid grid-cols-1 items-start gap-x-8 gap-y-3 lg:grid-cols-[minmax(15rem,2fr)_3fr]"
			onsubmit={(e) => {
				e.preventDefault();
				void create();
			}}
		>
			<div class="flex flex-col gap-3">
				<label class="flex flex-col gap-1 text-sm">
					<span>Slug</span>
					<Input bind:value={slug} placeholder="vasa-portraits" required pattern="[a-z0-9-]+" />
				</label>
				<label class="flex flex-col gap-1 text-sm">
					<span>Title</span>
					<Input bind:value={title} placeholder="Vasa portraits" />
				</label>
				<label class="flex flex-col gap-1 text-sm">
					<span>Description</span>
					<Textarea
						bind:value={description}
						rows={3}
						placeholder="What is being labelled, and why"
					/>
				</label>
				<label class="flex flex-col gap-1 text-sm">
					<span
						>Instructions <span class="text-muted-foreground"
							>(shown to annotators — how to label)</span
						></span
					>
					<Textarea
						bind:value={instructions}
						rows={3}
						placeholder="Label every visible portrait; skip seals and marginalia."
					/>
				</label>
				<label class="flex flex-col gap-1 text-sm">
					<span
						>Consensus <span class="text-muted-foreground"
							>(annotators per item — 1 is ordinary, up to 5)</span
						></span
					>
					<Input type="number" bind:value={consensusN} min={1} max={5} step={1} />
				</label>
				<label class="flex items-center gap-2 text-sm">
					<Checkbox bind:checked={reviewRequired} />
					Review required before a task is accepted
				</label>
			</div>

			<div class="flex flex-col gap-3">
				<label class="flex flex-col gap-1 text-sm">
					<span
						>Template <span class="text-muted-foreground"
							>(a complete task — per-class tools, attributes, relations)</span
						></span
					>
					<Select
						bind:value={() => templateId, pickTemplate}
						ariaLabel="Task template"
						options={[
							{ value: 'custom', label: 'custom (type your own classes)' },
							{ value: 'yaml', label: 'custom (define in YAML)' },
							...PROJECT_TEMPLATES.map((t) => ({ value: t.id, label: t.name })),
						]}
					/>
				</label>
				{#if templateId !== 'custom'}
					<!-- THE TASK EDITOR — one working copy, two views. The FORM speaks plain language:
				     a LABEL is what an annotator marks; HOW it is captured is stated per label
				     (drawn on the canvas / ranges in the text / a whole-item choice); TRANSCRIBE
				     declares that its regions carry transcribed text (the primary content, not a
				     tool and not a field); FIELDS are the typed extras (reading order, script).
				     The YAML view is the same draft as text — the full-power surface and the
				     copy-out/paste-in exchange format. -->
					<div
						class="border-border bg-muted/40 flex flex-col gap-2 rounded-md border p-2 text-xs"
						data-testid="template-summary"
					>
						<div class="flex items-start justify-between gap-2">
							<p class="text-muted-foreground">
								{pickedTemplate?.description ?? 'A task defined from scratch, in YAML.'}
							</p>
							<!-- The estate's Tabs primitive, not hand-rolled toggle buttons: Bits UI owns the
						     selected state and the keyboard interaction. Controlled, because switching TO
						     the YAML view must serialize the current draft first. -->
							<Tabs.Root
								value={editorView}
								onValueChange={(view) => (view === 'yaml' ? showYaml() : (editorView = 'form'))}
							>
								<Tabs.List class="h-6" aria-label="Editor view">
									<Tabs.Trigger value="form" class="px-2 text-[10px]" data-testid="task-view-form">
										Form
									</Tabs.Trigger>
									<Tabs.Trigger value="yaml" class="px-2 text-[10px]" data-testid="task-view-yaml">
										YAML
									</Tabs.Trigger>
								</Tabs.List>
							</Tabs.Root>
						</div>
						{#if editorView === 'yaml'}
							<Textarea
								class="min-h-72 font-mono text-[11px] leading-relaxed"
								value={yamlText}
								spellcheck="false"
								aria-label="Task definition YAML"
								data-testid="task-yaml"
								oninput={(e) => onYamlInput(e.currentTarget.value)}
							/>
							{#if yamlErrors.length}
								<ul class="text-destructive flex flex-col gap-0.5" data-testid="task-yaml-errors">
									{#each yamlErrors as err, i (`${i}:${err}`)}<li>{err}</li>{/each}
								</ul>
							{:else}
								<p class="text-muted-foreground text-[10px]">
									draw = geometry on the canvas · span = ranges in the text · tag = whole-item
									choice · transcribe = regions carry transcribed text · fields = typed extras per
									region
								</p>
							{/if}
						{:else}
							{#each draft.classes as row, i (i)}
								<div
									class="border-border/60 flex flex-col gap-1 rounded border-b pb-1.5 last:border-b-0"
									data-testid="template-class-row"
								>
									<div class="flex flex-wrap items-center gap-1.5">
										<Input
											class="h-6 w-36 text-xs"
											bind:value={row.name}
											aria-label="Class name"
											placeholder="label name"
										/>
										<label class="text-muted-foreground flex items-center gap-1 text-[10px]">
											<Checkbox
												bind:checked={row.required}
												aria-label="Required on every item"
												class="size-3"
											/>
											required
										</label>
										<span class="flex-1"></span>
										<Button
											variant="ghost"
											size="icon-xs"
											title="Remove this label"
											data-testid={`remove-class-${i}`}
											onclick={() => (draft.classes = draft.classes.filter((_, j) => j !== i))}
										>
											✕
										</Button>
									</div>
									<div class="flex flex-wrap items-center gap-1">
										<span class="text-muted-foreground w-14 shrink-0 text-[10px]">drawn as</span>
										{#each DRAW_TOOLS as tool (tool)}
											<Button
												variant={row.draw.includes(tool) ? 'secondary' : 'ghost'}
												size="xs"
												class="h-5 px-1.5 text-[10px]"
												aria-pressed={row.draw.includes(tool)}
												data-testid={`class-${i}-tool-${tool}`}
												onclick={() => toggleDraw(row, tool)}
											>
												{tool}
											</Button>
										{/each}
										<span class="text-muted-foreground px-0.5">·</span>
										<Button
											variant={row.transcribe ? 'secondary' : 'ghost'}
											size="xs"
											class="h-5 px-1.5 text-[10px]"
											aria-pressed={row.transcribe}
											title="Each region of this label carries transcribed text"
											data-testid={`class-${i}-cap-transcribe`}
											onclick={() => (row.transcribe = !row.transcribe)}
										>
											transcribe
										</Button>
										<Button
											variant={row.span ? 'secondary' : 'ghost'}
											size="xs"
											class="h-5 px-1.5 text-[10px]"
											aria-pressed={row.span}
											title="Marks character ranges in the text (NER)"
											data-testid={`class-${i}-cap-span`}
											onclick={() => (row.span = !row.span)}
										>
											span
										</Button>
										<Button
											variant={row.tag ? 'secondary' : 'ghost'}
											size="xs"
											class="h-5 px-1.5 text-[10px]"
											aria-pressed={row.tag}
											title="A whole-item choice — the classification chip bar"
											data-testid={`class-${i}-cap-tag`}
											onclick={() => (row.tag = !row.tag)}
										>
											tag
										</Button>
									</div>
									{#if row.fields.length}
										<div class="flex flex-wrap items-center gap-1">
											<span class="text-muted-foreground w-14 shrink-0 text-[10px]">fields</span>
											{#each row.fields as field (field.name)}
												{@const detail = `${field.options.length ? field.options.join(' | ') : field.type}${field.required ? ' · required' : ''}`}
												<span
													class="border-border bg-background inline-flex items-center gap-1 rounded border px-1 py-0.5 text-[10px]"
													title="A typed per-region field — edit in the YAML view"
												>
													{field.name}
													<span class="text-muted-foreground">{detail}</span>
												</span>
											{/each}
										</div>
									{/if}
								</div>
							{/each}
							<div class="flex items-center justify-between gap-2">
								<Button
									variant="outline"
									size="xs"
									data-testid="add-class"
									onclick={() =>
										(draft.classes = [
											...draft.classes,
											{
												name: '',
												draw: ['bbox'],
												span: false,
												tag: false,
												transcribe: false,
												required: false,
												fields: [],
											},
										])}
								>
									+ add label
								</Button>
								<span class="text-muted-foreground text-[10px]"
									>typed fields &amp; relations: the YAML view</span
								>
							</div>
						{/if}
						{#if draft.relations.length}
							<p class="text-muted-foreground" data-testid="template-relations">
								relations: {liveRelations.length
									? liveRelations.map((r) => r.name).join(', ')
									: 'none'}
								{#if liveRelations.length < draft.relations.length}
									<span class="text-warning">
										— {draft.relations.length - liveRelations.length} dropped (an endpoint class was renamed
										or removed)</span
									>
								{/if}
							</p>
						{/if}
					</div>
					<!-- LIVE, from the same draft either view edits — "customize how the labeling task
					     is supposed to look" needs to SHOW the look while it is being customized. -->
					<TaskPreview {draft} />
				{:else}
					<label class="flex flex-col gap-1 text-sm">
						<span
							>Label classes <span class="text-muted-foreground"
								>(comma-separated — the labeling task)</span
							></span
						>
						<Input bind:value={classesText} placeholder="person, ship, signature" />
					</label>
				{/if}
				{#if templateId === 'custom'}
					<!-- The free-form knobs are the CUSTOM path's; a template (or the YAML editor)
				     already answered all three, and dead controls under either would advertise
				     overrides that do not apply. -->
					<label class="flex flex-col gap-1 text-sm">
						<span>Shape kind</span>
						<Select
							bind:value={shapeKind}
							ariaLabel="Shape kind"
							options={SHAPE_KINDS.map((kind) => ({ value: kind, label: kind }))}
						/>
					</label>
					<label class="flex flex-col gap-1 text-sm">
						<span
							>Task type <span class="text-muted-foreground"
								>(seeds each class's tools — enforced at submit)</span
							></span
						>
						<Select
							bind:value={taskKind}
							ariaLabel="Task type"
							options={[
								{ value: 'free', label: 'free (no task type)' },
								...Object.keys(TASK_PRESETS).map((kind) => ({ value: kind, label: kind })),
							]}
						/>
					</label>
					<label class="flex items-center gap-2 text-sm">
						<Checkbox bind:checked={classesRequired} aria-label="Every class is required" />
						<span
							>Every class is required <span class="text-muted-foreground"
								>(each must appear on every completed item)</span
							></span
						>
					</label>
				{/if}
			</div>

			<div class="border-border col-span-full flex items-center justify-end gap-2 border-t pt-3">
				{#if createError}
					<p class="text-destructive mr-auto text-sm">{createError}</p>
				{/if}
				<Button type="button" variant="outline" onclick={() => (createOpen = false)}>Cancel</Button>
				<Button type="submit" disabled={creating || !slug.trim() || yamlErrors.length > 0}>
					{creating ? 'Creating…' : 'Create project'}
				</Button>
			</div>
		</form>
	</Card>
{/snippet}
