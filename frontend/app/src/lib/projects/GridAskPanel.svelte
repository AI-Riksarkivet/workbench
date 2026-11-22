<script lang="ts">
	// THE GRID'S "ASK A MODEL": the models the deployment's endpoint serves, read when the person opens them, those that
	// propose anything and the grid can ask about many items at once (`gridAsk`), with the one prompt of words they take,
	// each judged against the items it would be asked about (`fits`), since the items of one project may have been sent
	// with different classes. Picking one hands it and the words to the grid (`onask`), which asks each item.
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import * as Popover from '@riksarkivet/ui/popover';
	import { Sparkles } from '@lucide/svelte';

	import { listModels, proposes, type AssistModel } from '#lib/api/assist.api.ts';
	import { fits, gridAsk } from './label-grid.ts';
	import { sentence } from './presentation.ts';
	import type { TaskDetail } from './types.ts';

	let {
		projectId,
		targets,
		onask,
		onclose,
	}: {
		projectId: string;
		/** The items an ask would run on, as the grid has read them: the selection, else the item in focus. */
		targets: readonly TaskDetail[];
		/** Ask ``model`` about the targets, with ``words`` where it takes them. */
		onask: (model: AssistModel, words: string) => void;
		/** After the panel closed, so the grid takes the focus back. */
		onclose: () => void;
	} = $props();

	let open = $state(false);
	let words = $state('');
	/** Whether the panel was ever opened: the models are read then, and held. */
	let opened = $state(false);
	const modelsRead = $derived(opened ? listModels({ project: projectId }) : null);
	const answer = $derived(modelsRead?.current);
	const listing = $derived(answer?.ok ? answer.data : null);
	const failure = $derived(answer && !answer.ok ? answer.detail : null);

	const askable = $derived(
		(listing?.models ?? []).filter((m) => proposes(m) && gridAsk(m) !== null),
	);
	const anyWords = $derived(askable.some((m) => gridAsk(m) === 'words'));
	/** How many of the targets take a shape each model returns, by its name. */
	const fitting = $derived(
		new Map(askable.map((m) => [m.name, targets.filter((t) => fits(m, t)).length])),
	);
	/** Whether ``model`` may be asked now: an item to ask, one that takes what it returns, and its words where it takes
	 *  them. */
	const ready = (model: AssistModel): boolean =>
		(fitting.get(model.name) ?? 0) > 0 && (gridAsk(model) !== 'words' || words.trim() !== '');
	/** The model Enter in the words asks: the first listed that takes words and may be asked now. */
	const enterModel = $derived(askable.find((m) => gridAsk(m) === 'words' && ready(m)) ?? null);

	function ask(model: AssistModel): void {
		open = false;
		onask(model, words);
	}
	/** Opened, the models are read the first time, and again after a read that failed. */
	function onOpenChange(now: boolean): void {
		if (!now) return;
		if (failure !== null) void modelsRead?.refresh();
		opened = true;
	}
	function onWordsKey(e: KeyboardEvent): void {
		if (e.key !== 'Enter' || e.isComposing) return;
		e.preventDefault();
		if (enterModel) ask(enterModel);
	}
	/** Back to the item in focus once the panel is closed, so the grid's keys (Alt+A, Alt+R, Ctrl+Z) go on where they
	 *  were. The panel's focus scope also lets go while it stays open (it is set up again when the panel's content
	 *  changes), and moving the focus then would scroll the page from it. */
	function onCloseAutoFocus(e: Event): void {
		e.preventDefault();
		if (!open) onclose();
	}
</script>

<Popover.Root bind:open {onOpenChange}>
	<Popover.Trigger>
		{#snippet child({ props })}
			<Button {...props} variant="outline" size="sm" data-testid="grid-assist-open">
				<Sparkles class="size-4" /> Ask a model
			</Button>
		{/snippet}
	</Popover.Trigger>
	<Popover.Content
		class="flex w-96 flex-col gap-3 p-3"
		align="start"
		data-testid="grid-assist-panel"
		{onCloseAutoFocus}
	>
		<h3 class="text-sm font-medium">
			Ask a model about {targets.length} item{targets.length === 1 ? '' : 's'}
		</h3>
		{#if failure}
			<p class="text-destructive text-xs" role="alert" data-testid="grid-assist-failed">
				The models could not be listed: {failure}
			</p>
		{:else if listing === null}
			<p class="text-muted-foreground text-xs" role="status">Reading the models…</p>
		{:else if !listing.available}
			<p class="text-muted-foreground text-xs" data-testid="grid-assist-unavailable">
				{sentence(listing.reason)}.
			</p>
		{:else if askable.length === 0}
			<p class="text-muted-foreground text-xs" data-testid="grid-assist-none">
				The model endpoint serves no model the grid can ask: one that needs a drawn box or clicks is
				asked on the canvas, one item at a time.
			</p>
		{:else}
			{#if anyWords}
				<label class="flex flex-col gap-1 text-xs">
					<span class="text-muted-foreground">What to find, on every item</span>
					<Input
						bind:value={words}
						placeholder="e.g. stamp"
						data-testid="grid-assist-prompt"
						onkeydown={onWordsKey}
					/>
					{#if enterModel}
						<span class="text-muted-foreground" data-testid="grid-assist-enter"
							>Enter finds it with {enterModel.title || enterModel.name}</span
						>
					{/if}
				</label>
			{/if}
			<ul class="flex flex-col gap-2">
				{#each askable as m (m.name)}
					{@const fit = fitting.get(m.name) ?? 0}
					<li
						class="flex flex-col gap-1 text-xs"
						data-testid="grid-assist-model"
						data-model={m.name}
					>
						<div class="flex items-center gap-2">
							<span class="min-w-0 flex-1 truncate font-medium" title={m.name}
								>{m.title || m.name}</span
							>
							{#if targets.length > 0 && fit < targets.length}
								<Badge
									variant="destructive"
									class="text-[10px]"
									title="What it returns ({m.returns.join(', ') ||
										'nothing named'}) is no shape the classes of {targets.length -
										fit} of these items take; those are not asked"
									data-testid="grid-assist-incompatible"
									>{fit === 0 ? 'does not fit' : `fits ${fit} of ${targets.length}`}</Badge
								>
							{/if}
							<Button size="xs" variant="outline" disabled={!ready(m)} onclick={() => ask(m)}
								>{gridAsk(m) === 'item' ? 'Ask' : 'Find'}</Button
							>
						</div>
						<span class="text-muted-foreground font-mono text-[11px]"
							>{m.name} · {m.version} · returns {m.returns.join(', ') || 'nothing named'}</span
						>
					</li>
				{/each}
			</ul>
			<p class="text-muted-foreground text-xs">
				Each item is asked on its own, claimed first where it is open, and its answer saved to it as
				proposals to keep or remove. An item whose classes take no shape a model returns is not
				asked.
			</p>
		{/if}
	</Popover.Content>
</Popover.Root>
