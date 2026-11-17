<script lang="ts">
	// A text item's labeling canvas: the document is the canvas (no picture, no time line). It is ready for a person's
	// input once its rows are read and no page transition covers it (the browser sends every press to the document root
	// while one runs, `#lib/view-transition.svelte.ts`). Its text is the item's row's text as the dataset holds it, until
	// a span is labelled in it or it is corrected: then a row of the item's own holds it (`source: dataset`), and the
	// spans range into that row. Other rows with text (a transcribed region, a line an
	// import brought) read as further lines. The task's text classes stand above it as the keys that label a selection.
	import { onDestroy, onMount } from 'svelte';
	import { loadAnnotations } from '#lib/api/annotations.api.ts';
	import { viewTransitions } from '#lib/view-transition.svelte.ts';
	import ClassPicker from '../ClassPicker.svelte';
	import { classesFor } from '../classes.ts';
	import type { AnnotatorController } from '../controller.svelte.ts';
	import TextLines from './TextLines.svelte';
	import { spansOf, textLines, type SpanMark, type TextLine } from './spans.ts';
	import { pointLength } from './codepoints.ts';

	let {
		controller,
		annotationsUrl,
		documentText,
		itemName,
		onload,
		onerror,
	}: {
		controller: AnnotatorController;
		annotationsUrl: string;
		/** The item's text as its dataset row holds it. */
		documentText: string;
		itemName: string;
		onload?: () => void;
		onerror?: (message: string, status?: number) => void;
	} = $props();

	let alive = true;
	let attached: unknown = null;
	onDestroy(() => {
		alive = false;
		if (controller.table !== null && controller.table === attached) controller.detachTable();
	});

	onMount(() => {
		void (async () => {
			const loaded = await loadAnnotations(annotationsUrl);
			if (!alive) return;
			if (!loaded.ok) {
				onerror?.(loaded.detail, loaded.status);
				return;
			}
			controller.attachTable(loaded.data.table, annotationsUrl, loaded.data.version);
			attached = controller.table;
			onload?.();
			region?.focus();
		})();
	});

	/** The document's text: the row holding it once there is one, else the dataset's. Then any other row's text. */
	const lines = $derived.by<TextLine[]>(() => {
		const held = textLines(controller.rows);
		const document = held.some((l) => {
			const row = controller.rows.find((r) => r.index === l.index);
			return row?.source === 'dataset';
		});
		return document || documentText === ''
			? held
			: [{ index: -1, id: '', text: documentText, label: '' }, ...held];
	});

	const classes = $derived(classesFor(controller.classDefs, 'text'));

	/** The compared version's spans, by the text of the line they range into. */
	const ghosts = $derived.by(() => {
		const out = new Map<string, SpanMark[]>();
		for (const entry of controller.compareDiff) {
			const theirs = entry.theirs;
			if (entry.kind === 'same' || !theirs?.span) continue;
			const marks = out.get(theirs.span.text) ?? [];
			marks.push({
				index: theirs.index,
				start: theirs.span.start,
				end: theirs.span.end,
				label: theirs.label,
			});
			out.set(theirs.span.text, marks);
		}
		return out;
	});

	let region = $state<HTMLDivElement | null>(null);
	let host = $state<HTMLDivElement | null>(null);
	/** Where the class picker opens: at the end of the span it labels. */
	let anchor = $state<{ x: number; y: number; width: number; height: number } | null>(null);
	$effect(() => {
		const open = controller.picker;
		void controller.rows;
		if (!open || !host) {
			anchor = null;
			return;
		}
		const tag = host.querySelector<HTMLElement>(`[data-span-end="${open.index}"]`);
		if (!tag) return;
		const at = tag.getBoundingClientRect();
		const origin = host.getBoundingClientRect();
		anchor = {
			x: at.left - origin.left + host.scrollLeft,
			y: at.top - origin.top + host.scrollTop,
			width: at.width,
			height: at.height,
		};
	});

	const spanCount = $derived(
		lines.reduce(
			(n, l) => n + (l.index >= 0 ? spansOf(controller.rows, l.id, pointLength(l.text)).length : 0),
			0,
		),
	);
</script>

<!-- The text canvas: one focusable region, its text selectable; the keys that label are the page's (`CanvasShell`). -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
	bind:this={region}
	class="focus-visible:ring-ring bg-background relative h-full w-full overflow-y-auto outline-none focus-visible:ring-2 focus-visible:ring-inset"
	role="region"
	aria-label={`Labeling canvas: ${itemName}`}
	aria-describedby="text-canvas-keys"
	tabindex="0"
	data-testid="text-canvas"
	data-ready={controller.table !== null && !viewTransitions.running ? 'true' : 'false'}
	aria-busy={controller.table === null || viewTransitions.running}
>
	<p id="text-canvas-keys" class="sr-only">
		Select text and release, or press Enter, to mark a span and pick its class; with text selected,
		a digit gives it that class at once. Alt with the left and right arrows moves the selected
		span's end, with Shift its start.
	</p>
	<div class="relative mx-auto flex max-w-3xl flex-col gap-2 px-6 py-4" bind:this={host}>
		{#if classes.length > 0}
			<div
				class="bg-background/95 sticky top-0 z-10 flex flex-wrap items-center gap-1.5 py-2 backdrop-blur"
				data-testid="text-classes"
			>
				{#each classes as c, i (c.name)}
					<span class="bg-card inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs">
						<span
							class="size-2.5 rounded-sm"
							style:background-color={controller.classColour.get(c.name) ??
								'var(--muted-foreground)'}
						></span>
						{#if i < 9}<kbd class="text-muted-foreground font-mono text-[10px]">{i + 1}</kbd>{/if}
						{c.name}
					</span>
				{/each}
				<span class="text-muted-foreground ml-auto text-xs" data-testid="text-span-count"
					>{spanCount} span{spanCount === 1 ? '' : 's'} · select text, then a digit or Enter</span
				>
			</div>
		{/if}
		{#if lines.length === 0}
			<p class="text-muted-foreground py-12 text-center text-sm">
				This item holds no text to label.
			</p>
		{:else}
			<TextLines {controller} {lines} {ghosts} large testid="text-document" />
		{/if}
		{#if anchor}
			<ClassPicker {controller} {anchor} onclose={() => region?.focus()} />
		{/if}
	</div>
</div>
