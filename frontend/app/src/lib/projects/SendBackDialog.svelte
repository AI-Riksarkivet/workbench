<script lang="ts">
	// THE NOTE A REVIEWER SENDS AN ITEM BACK WITH, one form wherever it is written (the Items table, the Label grid, the
	// canvas's side panel). A note typed is never lost by a stray key: Escape, a click outside or Cancel with text in it
	// asks before discarding it, while an empty form closes at once. A page may ask one more thing beside the note
	// (`question`: whether a judge's flag was right), which it holds itself.
	import { Button } from '@riksarkivet/ui/button';
	import { Dialog } from '@riksarkivet/ui/dialog';
	import { Textarea } from '@riksarkivet/ui/textarea';
	import type { Snippet } from 'svelte';

	let {
		open = $bindable(false),
		title,
		description,
		placeholder = '',
		busy = false,
		question,
		testid,
		onsend,
	}: {
		open?: boolean;
		title: string;
		description: string;
		placeholder?: string;
		/** A send in flight: the form waits for it. */
		busy?: boolean;
		/** One more thing the form asks under the note, held by the page. */
		question?: Snippet;
		testid: string;
		/** The note, trimmed. The form closes once it is taken: at once, or when the answer is true; an answer of false
		 *  (the item was not sent back) keeps the note in the form. */
		onsend: (note: string) => void | Promise<boolean>;
	} = $props();

	let note = $state('');
	/** True while the form asks whether to discard what is typed. */
	let confirming = $state(false);
	let field = $state<HTMLTextAreaElement | null>(null);

	function close(): void {
		note = '';
		confirming = false;
		open = false;
	}

	/** A request to close: at once when nothing is typed, else the question first. */
	function leave(e?: Event): void {
		e?.preventDefault();
		if (confirming) {
			// Escape on the question keeps the note: the person goes back to writing.
			confirming = false;
			field?.focus();
			return;
		}
		if (note.trim()) confirming = true;
		else close();
	}

	async function send(e: SubmitEvent): Promise<void> {
		e.preventDefault();
		const text = note.trim();
		if (!text || busy) return;
		const taken = await onsend(text);
		if (taken !== false) close();
	}
</script>

<Dialog.Root
	{open}
	onOpenChange={(next) => {
		if (next) open = true;
		else leave();
	}}
>
	<Dialog.Content
		class="sm:max-w-md"
		data-testid={testid}
		onEscapeKeydown={leave}
		onInteractOutside={leave}
	>
		<Dialog.Title>{title}</Dialog.Title>
		<Dialog.Description>{description}</Dialog.Description>
		<form class="flex flex-col gap-3" onsubmit={send}>
			<Textarea
				bind:ref={field}
				bind:value={note}
				rows={4}
				aria-label="What should change"
				{placeholder}
				readonly={confirming}
			/>
			{#if question}
				{@render question()}
			{/if}
			{#if confirming}
				<div
					class="border-warning bg-warning/10 flex flex-wrap items-center gap-2 border-l-2 px-2 py-1.5 text-sm"
					role="alert"
					data-testid="send-back-discard"
				>
					<span class="min-w-0 flex-1">Discard the note you wrote?</span>
					<Button
						type="button"
						size="sm"
						variant="outline"
						onclick={() => {
							confirming = false;
							field?.focus();
						}}
					>
						Keep writing
					</Button>
					<Button type="button" size="sm" variant="destructive" onclick={close}>Discard</Button>
				</div>
			{:else}
				<div class="flex justify-end gap-2">
					<Button type="button" variant="outline" onclick={() => leave()}>Cancel</Button>
					<Button type="submit" disabled={!note.trim() || busy}>Send back</Button>
				</div>
			{/if}
		</form>
	</Dialog.Content>
</Dialog.Root>
