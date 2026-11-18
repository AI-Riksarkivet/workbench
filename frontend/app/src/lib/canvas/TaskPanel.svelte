<script lang="ts">
	// The head of the canvas's side panel: what the person is doing with this item (labeling it, reviewing someone's
	// submission, waiting for a review, reading it), what still keeps it from being submitted, and the transitions they
	// may make now. A submit saves first; a review that sends the item back carries a note, which its annotator reads
	// here when they open it again.
	import { Check, CheckCheck, CornerUpLeft, Hand, Send, Undo } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';
	import { Dialog } from '@workbench/ui/dialog';
	import { Textarea } from '@workbench/ui/textarea';
	import { cn } from '@workbench/ui/utils';
	import { fireTaskEvent } from '#lib/api/projects.api.ts';
	import type { TaskDetail } from '#lib/projects/types.ts';
	import { submittable } from './classes.ts';
	import type { AnnotatorController } from './controller.svelte.ts';
	import { panelState, REVIEW_KEYS } from './task-panel.ts';
	import { keyIsNotTheCanvas } from './walk-keys.ts';

	let {
		controller,
		task,
		permissions,
		me,
		browsing = false,
		onchanged,
	}: {
		controller: AnnotatorController;
		task: TaskDetail;
		permissions: readonly string[];
		me: string | null;
		/** A browse pass: the panel says where the item stands, read-only, whatever its state, and offers nothing. */
		browsing?: boolean;
		/** The task after a transition, for the page to show. */
		onchanged: (task: TaskDetail) => void;
	} = $props();

	const panel = $derived(panelState(task, permissions, me, { browsing }));
	const blockers = $derived(controller.blockers);
	const ready = $derived(submittable(blockers));
	const rowNumber = (index: number): string =>
		`#${controller.reviewQueue.findIndex((r) => r.index === index) + 1}`;

	let busy = $state<string | null>(null);
	/** What the last action here refused, with the save the canvas had made when it did: a later save that lands
	 *  answers it, so it is shown only until then. */
	let refused = $state<{ text: string; savedAt: number | null } | null>(null);
	const failure = $derived(
		refused !== null && refused.savedAt === controller.lastSavedAt ? refused.text : null,
	);
	const refuse = (text: string): void => {
		refused = { text, savedAt: controller.lastSavedAt };
	};
	let noteOpen = $state(false);
	let note = $state('');

	async function fire(event: string, message?: string): Promise<void> {
		if (busy) return;
		busy = event;
		refused = null;
		if (event === 'submit' || event === 'fix_and_accept') {
			// What is submitted (or accepted with a reviewer's fixes) is what is saved: the item's unsaved edits go first,
			// and a save that fails stops the transition.
			await controller.save();
			if (controller.dirty) {
				busy = null;
				refuse(
					`Not ${event === 'submit' ? 'submitted' : 'accepted'}: ${controller.saveError ?? 'the edits were not saved'}.`,
				);
				return;
			}
		}
		const result = await fireTaskEvent({
			taskId: task.task_id,
			event,
			...(message ? { message } : {}),
		});
		busy = null;
		if (result.ok) {
			noteOpen = false;
			note = '';
			onchanged(result.data);
		} else refuse(result.detail);
	}

	const REVIEW_HINTS: Record<string, string> = {
		accept: 'Accept the work as it was submitted (Shift+A)',
		fix_and_accept: 'Save the fixes you made here and accept the work with them (Shift+F)',
		request_changes: 'Send it back to its annotator with a note (Shift+R)',
	};
	const LABELS: Record<string, string> = {
		submit: 'Submit for review',
		release: 'Put back',
		accept: 'Accept',
		fix_and_accept: 'Accept with my fixes',
		request_changes: 'Send back…',
		claim: 'Claim',
	};
	/** What the unclassed marks are called: spans of a text, regions of a picture or a recording, marks when both. */
	/** A reviewer's verdict by key, from anywhere on the page but a field: Shift+A accepts, Shift+F accepts with the
	 *  fixes made here, Shift+R opens the note that sends it back. Only what the panel offers. */
	function onKeydown(e: KeyboardEvent): void {
		if (panel.mode !== 'reviewing' || !e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
		if (keyIsNotTheCanvas(e)) return;
		const event = REVIEW_KEYS[e.key.toLowerCase()];
		if (!event || !panel.actions.includes(event) || noteOpen) return;
		e.preventDefault();
		if (event === 'request_changes') noteOpen = true;
		else void fire(event);
	}
	const unlabelledNoun = $derived.by(() => {
		const spans = blockers.unlabelled.filter((index) => {
			const row = controller.rows.find((r) => r.index === index);
			return row?.shape === 'text' && row.parentId !== '';
		}).length;
		return spans === 0 ? 'region' : spans === blockers.unlabelled.length ? 'span' : 'mark';
	});
</script>

<svelte:window onkeydown={onKeydown} />

<section
	class="border-border flex flex-col gap-2 border-b px-3 py-2.5"
	aria-labelledby="task-panel-title"
	data-testid="task-panel"
	data-mode={panel.mode}
>
	<div>
		<h2 id="task-panel-title" class="text-sm font-semibold" data-testid="task-panel-title">
			{panel.title}
		</h2>
		<p class="text-muted-foreground text-xs">{panel.detail}</p>
	</div>

	{#if panel.note}
		<blockquote
			class="border-warning bg-warning/10 rounded-md border-l-2 px-2 py-1.5 text-xs"
			data-testid="review-note"
		>
			<span class="font-medium">{panel.note.by}:</span>
			{panel.note.message}
		</blockquote>
	{/if}

	{#if panel.mode === 'annotating' && controller.classDefs.length > 0}
		<ul
			class="flex flex-col gap-0.5 text-xs"
			data-testid="submit-check"
			aria-label="Before submitting"
		>
			{#if blockers.empty}
				<li class="text-destructive">
					{task.media.kind === 'text'
						? "Nothing is marked yet: mark the item's spans first."
						: "Nothing is labeled yet: draw the item's regions first."}
				</li>
			{/if}
			{#if blockers.unlabelled.length > 0}
				<li class="text-destructive">
					{blockers.unlabelled.length}
					{unlabelledNoun}{blockers.unlabelled.length === 1 ? '' : 's'} without a class:
					{#each blockers.unlabelled.slice(0, 8) as index, i (index)}
						{i > 0 ? ', ' : ''}<button
							type="button"
							class="underline underline-offset-2"
							onclick={() => controller.select(index)}>{rowNumber(index)}</button
						>
					{/each}{blockers.unlabelled.length > 8 ? ' …' : ''}
				</li>
			{/if}
			{#if blockers.missing.length > 0}
				<li class="text-destructive">
					Required, not on the item yet: {blockers.missing.join(', ')}
				</li>
			{/if}
			{#if ready}
				<li class="text-success">
					Every region has a class{controller.classDefs.some((c) => c.required)
						? ' and every required class is here'
						: ''}.
				</li>
			{/if}
		</ul>
	{/if}

	{#if panel.actions.length > 0}
		<div class="flex flex-wrap gap-1.5">
			{#each panel.actions as event (event)}
				<Button
					size="sm"
					variant={event === 'submit' || event === 'accept' || event === 'claim'
						? 'default'
						: 'outline'}
					class={cn(event === 'submit' && 'flex-1')}
					disabled={busy !== null || (event === 'submit' && !ready)}
					title={event === 'submit' && !ready
						? 'Fix what the list above names first'
						: REVIEW_HINTS[event]}
					data-testid={`task-${event}`}
					onclick={() => (event === 'request_changes' ? (noteOpen = true) : void fire(event))}
				>
					{#if event === 'submit'}<Send class="size-3.5" />
					{:else if event === 'accept'}<Check class="size-3.5" />
					{:else if event === 'fix_and_accept'}<CheckCheck class="size-3.5" />
					{:else if event === 'request_changes'}<CornerUpLeft class="size-3.5" />
					{:else if event === 'claim'}<Hand class="size-3.5" />
					{:else}<Undo class="size-3.5" />{/if}
					{busy === event ? 'Working…' : (LABELS[event] ?? event)}
				</Button>
			{/each}
		</div>
	{/if}
	{#if failure}
		<p class="text-destructive text-xs" role="alert" data-testid="task-failure">{failure}</p>
	{/if}
</section>

<Dialog.Root bind:open={noteOpen}>
	<Dialog.Content class="sm:max-w-md" data-testid="send-back-dialog">
		<Dialog.Title>Send the item back</Dialog.Title>
		<Dialog.Description
			>Say what should change: the note goes to its annotator with the item.</Dialog.Description
		>
		<form
			class="flex flex-col gap-3"
			onsubmit={(e) => {
				e.preventDefault();
				if (note.trim()) void fire('request_changes', note.trim());
			}}
		>
			<Textarea
				bind:value={note}
				rows={4}
				aria-label="What should change"
				placeholder="The second box cuts off the signature."
			/>
			<div class="flex justify-end gap-2">
				<Button type="button" variant="outline" onclick={() => (noteOpen = false)}>Cancel</Button>
				<Button type="submit" disabled={!note.trim() || busy !== null}>Send back</Button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>
