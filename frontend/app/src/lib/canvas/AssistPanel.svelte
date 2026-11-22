<script lang="ts">
	// Assist: ask the deployment's model endpoint, through the annotator, for proposals on the item. The annotator says
	// whether the deployment names an endpoint and, when it does, which models it serves (each by its title, name and
	// version), which prompts each takes and whether what it returns fits the task. What the panel offers comes from
	// those prompts: a model that takes words is asked with the words to find ("Find", over the whole picture), one that
	// takes a box or clicks is armed, so a box drawn on the picture or each click is its prompt ("this", or with − "not
	// this"), every answer of a click session replacing the last until Done, and one that takes no prompt answers from the
	// item alone ("Ask"). Proposals arrive as `prediction` rows for
	// the person to keep (A) or reject (R), or all at once (Alt+A, Alt+R), each naming its model, version and score. An
	// ask that fails is said here in the annotator's words, never as an answer with nothing in it. A picture, or a
	// video's frame, is what a model looks at: on a text or a recording the panel says so instead.
	import { Check, Minus, Plus, Sparkles, X } from '@lucide/svelte';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import * as Popover from '@riksarkivet/ui/popover';
	import { Slider } from '@riksarkivet/ui/slider';
	import {
		fetchModels,
		proposes,
		takes,
		type AssistListing,
		type AssistModel,
	} from '#lib/api/assist.api.ts';
	import { sentence } from '#lib/projects/presentation.ts';
	import type { AnnotatorController } from './controller.svelte.ts';

	let {
		controller,
		kind,
	}: { controller: AnnotatorController; kind: 'image' | 'audio' | 'video' | 'text' } = $props();

	let open = $state(false);
	let listing = $state.raw<AssistListing | null>(null);
	let failure = $state<string | null>(null);
	const visual = $derived(kind === 'image' || kind === 'video');
	const armed = $derived(controller.assistModel);
	const SignIcon = $derived(controller.assistPointPositive ? Plus : Minus);

	async function read(): Promise<void> {
		const unit = controller.unit;
		if (unit === null || listing !== null) return;
		failure = null;
		const answer = await fetchModels(unit.project, unit.task);
		if (answer.ok) listing = answer.data;
		else failure = answer.detail;
	}

	/** Every model the endpoint lists that proposes anything (one that returns nothing is a project's judge only): one
	 *  that takes no prompt answers from the item alone, and is asked as it is. */
	const offered = $derived((listing?.models ?? []).filter(proposes));
	const promptless = (m: AssistModel): boolean => m.prompts.length === 0;
	const anyWords = $derived(offered.some((m) => takes(m, 'text')));

	function find(m: AssistModel): void {
		controller.setAssistModel(null);
		void controller.assist({ model: m });
		open = false;
	}

	function arm(m: AssistModel): void {
		controller.setAssistModel(m);
		open = false;
	}

	/** What arming a model asks of the person next, by the prompts it takes. */
	const armWord = (m: AssistModel): string =>
		takes(m, 'points') && takes(m, 'box') ? 'Box or click' : takes(m, 'points') ? 'Click' : 'Box';
</script>

<div class="flex shrink-0 items-center gap-1" data-testid="assist">
	<Popover.Root bind:open onOpenChange={(o) => o && void read()}>
		<Popover.Trigger>
			{#snippet child({ props })}
				<Button
					{...props}
					variant={armed ? 'secondary' : 'ghost'}
					size="sm"
					disabled={controller.readOnly !== null}
					data-testid="assist-open"
				>
					<Sparkles class="size-4" /> Assist
				</Button>
			{/snippet}
		</Popover.Trigger>
		<Popover.Content class="flex w-96 flex-col gap-3 p-3" data-testid="assist-panel">
			<h3 class="text-sm font-medium">Ask a model for proposals</h3>
			{#if !visual}
				<p class="text-muted-foreground text-xs" data-testid="assist-not-here">
					A model proposes regions of a picture or a video's frame; this item is {kind === 'text'
						? 'a text'
						: 'a recording'}, so there is nothing for it to look at here.
				</p>
			{:else if failure}
				<p class="text-destructive text-xs" role="alert" data-testid="assist-list-failed">
					The models could not be listed: {failure}
				</p>
			{:else if listing === null}
				<p class="text-muted-foreground text-xs" role="status">Reading the models…</p>
			{:else if !listing.available}
				<p class="text-muted-foreground text-xs" data-testid="assist-unavailable">
					{sentence(listing.reason)}.
				</p>
			{:else if offered.length === 0}
				<p class="text-muted-foreground text-xs" data-testid="assist-no-models">
					The model endpoint serves no model.
				</p>
			{:else}
				{#if anyWords}
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">What to find</span>
						<Input
							bind:value={controller.assistPrompt}
							placeholder="e.g. stamp"
							data-testid="assist-prompt"
							onkeydown={(e) => e.stopPropagation()}
						/>
					</label>
				{/if}
				<ul class="flex flex-col gap-2">
					{#each offered as m (m.name)}
						<li class="flex flex-col gap-1 text-xs" data-testid="assist-model" data-model={m.name}>
							<div class="flex items-center gap-2">
								<span class="min-w-0 flex-1 truncate font-medium" title={m.name}
									>{m.title || m.name}</span
								>
								{#if m.compatible === false}
									<Badge
										variant="destructive"
										class="text-[10px]"
										title="What it returns ({m.returns.join(
											', ',
										)}) is no shape this task's classes take"
										data-testid="assist-incompatible">does not fit this task</Badge
									>
								{/if}
								{#if promptless(m)}
									<Button
										size="xs"
										variant="outline"
										disabled={controller.assistBusy}
										onclick={() => find(m)}>Ask</Button
									>
								{/if}
								{#if takes(m, 'text')}
									<Button
										size="xs"
										variant="outline"
										disabled={!controller.assistPrompt.trim() || controller.assistBusy}
										onclick={() => find(m)}>Find</Button
									>
								{/if}
								{#if takes(m, 'box') || takes(m, 'points')}
									<Button
										size="xs"
										variant="outline"
										disabled={controller.assistBusy}
										onclick={() => arm(m)}
										data-testid="assist-arm">{armWord(m)}</Button
									>
								{/if}
							</div>
							<span
								class="text-muted-foreground font-mono text-[11px]"
								data-testid="assist-model-version"
								>{m.name} · {m.version} · returns {m.returns.join(', ') || 'nothing named'}</span
							>
						</li>
					{/each}
				</ul>
				<label class="flex flex-col gap-1 text-xs">
					<span class="text-muted-foreground"
						>Keep proposals scored at least {controller.assistMinConfidence.toFixed(2)}</span
					>
					<Slider
						min={0}
						max={1}
						step={0.05}
						value={controller.assistMinConfidence}
						aria-label="The lowest score a proposal is kept at"
						onValueChange={(value) => (controller.assistMinConfidence = value)}
					/>
				</label>
			{/if}
		</Popover.Content>
	</Popover.Root>
	{#if armed}
		<!-- The armed model, its session's sign and its end, on the bar while the canvas takes its clicks. -->
		<span
			class="bg-secondary flex items-center gap-1 rounded-md px-2 py-0.5 text-xs"
			data-testid="assist-armed"
		>
			<Sparkles class="size-3" />
			{armed.title || armed.name}{controller.assistPoints.length > 0
				? ` · ${controller.assistPoints.length} point${controller.assistPoints.length === 1 ? '' : 's'}`
				: ''}
			{#if takes(armed, 'points')}
				<Button
					variant="ghost"
					size="icon-xs"
					aria-pressed={!controller.assistPointPositive}
					aria-label={controller.assistPointPositive
						? 'The next click says “this”: switch to “not this”'
						: 'The next click says “not this”: switch to “this”'}
					onclick={() => (controller.assistPointPositive = !controller.assistPointPositive)}
					data-testid="assist-sign"
				>
					<SignIcon class="size-3" />
				</Button>
			{/if}
			<Button
				variant="ghost"
				size="icon-xs"
				aria-label="Done: keep this answer"
				onclick={() => controller.finishAssistSession()}
				data-testid="assist-done"><Check class="size-3" /></Button
			>
			<Button
				variant="ghost"
				size="icon-xs"
				aria-label="Put the model away"
				onclick={() => controller.setAssistModel(null)}
				data-testid="assist-disarm"><X class="size-3" /></Button
			>
		</span>
	{/if}
	{#if controller.assistAsking}
		<span class="text-muted-foreground text-xs" data-testid="assist-asking"
			>Asking {controller.assistAsking}…</span
		>
	{:else if controller.assistError}
		<!-- What the last ask met, in the annotator's words: an error, not an answer with nothing in it. The canvas's live
		     region says it as it happens; this stays until the next ask, or until put away. -->
		<span
			class="text-destructive flex max-w-md items-center gap-1 text-xs"
			data-testid="assist-error"
		>
			<span class="min-w-0 truncate" title={controller.assistError}
				>Assist failed: {controller.assistError}</span
			>
			<Button
				variant="ghost"
				size="icon-xs"
				aria-label="Dismiss"
				onclick={() => (controller.assistError = null)}><X class="size-3" /></Button
			>
		</span>
	{/if}
</div>
