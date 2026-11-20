<script lang="ts">
	// The item's lines of text as a span-labeling surface (the doccano interaction): each line reads at full size, its
	// spans underlined in their class's colour, one lane under another where they overlap, with the class at each span's
	// end. Selecting a stretch of a line makes a span there and opens the class picker on it (a digit labels a selection
	// directly); a span pressed is selected, and its edges are dragged by their handles or moved with Alt+←/→ (the end)
	// and Alt+Shift+←/→ (the start). A line's text is corrected in place, and the spans in it follow the words they
	// marked (`../span-remap.ts`). Another version's spans, when one is compared, are drawn dashed under the line's own.
	//
	// Offsets are read from the DOM by each stretch's `data-offset`: the text nodes inside a line are only its stretches,
	// so a selection's ends map to characters of the line whatever else (class tags, handles) is drawn between them.
	import { Check, Pencil, X } from '@lucide/svelte';
	import { untrack } from 'svelte';
	import { on } from 'svelte/events';
	import { Button } from '@riksarkivet/ui/button';
	import { Textarea } from '@riksarkivet/ui/textarea';
	import { cn } from '@riksarkivet/ui/utils';
	import type { AnnotatorController } from '../controller.svelte.ts';
	import {
		movedEdge,
		segmentText,
		spanLanes,
		spansOf,
		type SpanMark,
		type TextLine,
	} from './spans.ts';
	import { pointAt, pointLength, toPoints } from './codepoints.ts';

	let {
		controller,
		lines,
		ghosts = new Map(),
		large = false,
		testid = 'text-lines',
	}: {
		controller: AnnotatorController;
		lines: readonly TextLine[];
		/** Another version's spans per line (by the line's text), drawn dashed. */
		ghosts?: ReadonlyMap<string, readonly SpanMark[]>;
		/** The item is its text (a document read at reading size), not a lane under a picture or a recording. */
		large?: boolean;
		testid?: string;
	} = $props();

	const LANE = 5;
	const editing = $derived(controller.readOnly === null);
	const colour = (label: string): string =>
		controller.classColour.get(label) ?? 'var(--muted-foreground)';

	/** A handle's drag in progress: the span, its edge and where the edges stand now. */
	let drag = $state<{ index: number; edge: 'start' | 'end'; start: number; end: number } | null>(
		null,
	);

	function marksOf(line: TextLine): SpanMark[] {
		const marks = line.index < 0 ? [] : spansOf(controller.rows, line.id, pointLength(line.text));
		const d = drag;
		return d
			? marks.map((m) => (m.index === d.index ? { ...m, start: d.start, end: d.end } : m))
			: marks;
	}

	/** A point in the DOM as a character of its line, or null outside any line. A point inside a stretch is its
	 *  character; one between stretches (on a class tag, a handle, or the line itself) is the edge of the next stretch,
	 *  or the line's end when none follows: browsers put a selection's ends at such points. */
	function offsetAt(node: Node | null, at: number): number | null {
		if (node === null) return null;
		const element = node instanceof Element ? node : node.parentElement;
		const lineEl = element?.closest<HTMLElement>('[data-line]');
		if (!lineEl) return null;
		for (const stretch of lineEl.querySelectorAll<HTMLElement>('[data-offset]')) {
			const base = Number(stretch.dataset.offset);
			const span = document.createRange();
			span.selectNodeContents(stretch);
			const where = span.comparePoint(node, at);
			if (where < 0) return base;
			if (where > 0) continue;
			// The DOM counts UTF-16 units; a span counts code points (`./codepoints.ts`).
			if (node instanceof Text && stretch.contains(node)) return base + toPoints(node.data, at);
			return base + (at > 0 ? pointLength(stretch.textContent ?? '') : 0);
		}
		const text = lines[Number(lineEl.dataset.line)]?.text;
		return text === undefined ? null : pointLength(text);
	}

	/** The selection inside one line, trimmed of the spaces at its ends, or null. */
	function selected(): { line: TextLine; start: number; end: number } | null {
		const sel = window.getSelection();
		if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
		const range = sel.getRangeAt(0);
		const host = (n: Node) =>
			(n instanceof Element ? n : n.parentElement)?.closest<HTMLElement>('[data-line]') ?? null;
		const lineEl = host(range.startContainer);
		if (!lineEl || lineEl !== host(range.endContainer)) return null;
		const line = lines[Number(lineEl.dataset.line)];
		const a = offsetAt(range.startContainer, range.startOffset);
		const b = offsetAt(range.endContainer, range.endOffset);
		if (!line || a === null || b === null) return null;
		let start = Math.min(a, b);
		let end = Math.max(a, b);
		while (start < end && /\s/u.test(pointAt(line.text, start))) start += 1;
		while (end > start && /\s/u.test(pointAt(line.text, end - 1))) end -= 1;
		return end > start ? { line, start, end } : null;
	}

	function onselectionchange(): void {
		const chosen = selected();
		// Text being selected is what a class key labels next, not the annotation selected before.
		if (chosen !== null && controller.selectedIndex !== null) controller.select(null);
		controller.textSelection = chosen
			? {
					line: chosen.line.index >= 0 ? chosen.line.index : { text: chosen.line.text },
					start: chosen.start,
					end: chosen.end,
				}
			: null;
	}

	/** A press let go of on a line: over a selected stretch, a span there with its picker open; with nothing selected,
	 *  the shortest span under the pointer is picked. */
	function onpointerup(e: PointerEvent, at: TextLine): void {
		if (drag !== null) return;
		const chosen = editing ? selected() : null;
		if (!chosen) {
			const stretch = (e.target as Element | null)?.closest<HTMLElement>('[data-offset]');
			if (stretch && window.getSelection()?.isCollapsed !== false) {
				const offset = Number(stretch.dataset.offset);
				pick(marksOf(at).filter((m) => m.start <= offset && m.end > offset));
			}
			return;
		}
		window.getSelection()?.removeAllRanges();
		controller.textSelection = null;
		const line = chosen.line.index >= 0 ? chosen.line.index : { text: chosen.line.text };
		controller.addTextSpan(line, chosen.start, chosen.end);
	}

	/** A press on a stretch with no selection picks the shortest span over it. */
	function pick(covering: readonly SpanMark[]): void {
		if (controller.picker !== null) return;
		const shortest = covering.toSorted((a, b) => a.end - a.start - (b.end - b.start))[0];
		if (shortest) controller.select(shortest.index);
	}

	function caretAt(x: number, y: number): { node: Node; offset: number } | null {
		const doc = document as Document & {
			caretPositionFromPoint?: (
				x: number,
				y: number,
			) => { offsetNode: Node; offset: number } | null;
		};
		const position = doc.caretPositionFromPoint?.(x, y);
		if (position) return { node: position.offsetNode, offset: position.offset };
		const range = document.caretRangeFromPoint?.(x, y);
		return range ? { node: range.startContainer, offset: range.startOffset } : null;
	}

	function grab(e: PointerEvent, line: TextLine, mark: SpanMark, edge: 'start' | 'end'): void {
		if (!editing) return;
		e.preventDefault();
		e.stopPropagation();
		drag = { index: mark.index, edge, start: mark.start, end: mark.end };
		const move = (ev: PointerEvent) => {
			const d = drag;
			const caret = caretAt(ev.clientX, ev.clientY);
			const at = caret ? offsetAt(caret.node, caret.offset) : null;
			if (!d || at === null) return;
			const next = movedEdge({ start: d.start, end: d.end }, d.edge, at, pointLength(line.text));
			if (next) drag = { ...d, ...next };
		};
		// On the window: the handle is drawn again as the edge moves, so it cannot hold the pointer.
		const stopMove = on(window, 'pointermove', move);
		const stopUp = on(window, 'pointerup', () => {
			const d = drag;
			drag = null;
			stopMove();
			stopUp();
			if (d) controller.resizeSpan(d.index, d.start, d.end);
		});
	}

	/** The underlines a stretch carries: its spans' and the compared version's, one lane each, the selected span tinted. */
	function stretchStyle(
		covering: readonly SpanMark[],
		ghostCovering: readonly SpanMark[],
		lanes: ReadonlyMap<number, number>,
		ghostLanes: ReadonlyMap<number, number>,
		laneCount: number,
	): { image: string; size: string; position: string } {
		const image: string[] = [];
		const size: string[] = [];
		const position: string[] = [];
		for (const m of covering) {
			const c = colour(m.label);
			image.push(`linear-gradient(${c}, ${c})`);
			size.push('100% 3px');
			position.push(`0 calc(100% - ${(lanes.get(m.index) ?? 0) * LANE}px)`);
			if (m.index === controller.selectedIndex) {
				image.push(
					`linear-gradient(color-mix(in oklab, ${c} 22%, transparent), color-mix(in oklab, ${c} 22%, transparent))`,
				);
				size.push('100% 100%');
				position.push('0 0');
			}
		}
		for (const m of ghostCovering) {
			const c = colour(m.label);
			image.push(`repeating-linear-gradient(90deg, ${c} 0 4px, transparent 4px 7px)`);
			size.push('100% 2px');
			position.push(`0 calc(100% - ${(laneCount + (ghostLanes.get(m.index) ?? 0)) * LANE}px)`);
		}
		return { image: image.join(', '), size: size.join(', '), position: position.join(', ') };
	}

	/** The line being corrected, and its text as typed so far. */
	let fixing = $state<{ key: string; text: string } | null>(null);

	function correct(line: TextLine, text: string): void {
		fixing = null;
		if (text === line.text) return;
		if (line.index >= 0) controller.updateField(line.index, 'text', text);
		else controller.ensureDocument(text);
	}

	const lineKey = (line: TextLine): string => (line.index >= 0 ? line.id : 'document');

	let root = $state<HTMLDivElement | null>(null);
	// An annotation chosen (in the list, on the canvas) lets go of text selected in these lines.
	$effect(() => {
		if (controller.selectedIndex === null) return;
		untrack(() => {
			const sel = window.getSelection();
			if (sel && !sel.isCollapsed && root?.contains(sel.anchorNode)) sel.removeAllRanges();
		});
	});
</script>

{#snippet handle(line: TextLine, m: SpanMark, edge: 'start' | 'end')}<span
		class="relative inline-block w-0 select-none"
		aria-hidden="true"
		data-testid={`span-handle-${edge}`}
		onpointerdown={(e) => grab(e, line, m, edge)}
	><span class="bg-primary absolute -top-6 -left-1 h-2.5 w-2 cursor-ew-resize rounded-full"></span></span>{/snippet}

<svelte:document {onselectionchange} />

<div class="flex flex-col gap-1" data-testid={testid} bind:this={root}>
	{#each lines as line, n (lineKey(line))}
		{@const marks = marksOf(line)}
		{@const lanes = spanLanes(marks)}
		{@const laneCount = Math.max(1, ...[...lanes.values()].map((l) => l + 1))}
		{@const ghostMarks = ghosts.get(line.text) ?? []}
		{@const ghostLanes = spanLanes(ghostMarks)}
		{@const depth =
			laneCount + (ghostMarks.length > 0 ? Math.max(...[...ghostLanes.values()]) + 1 : 0)}
		{@const ends = new Map(marks.map((m) => [m.end, marks.filter((x) => x.end === m.end)]))}
		<div
			class={cn(
				'group flex items-start gap-2 rounded-md px-1',
				line.index >= 0 && controller.selectedIndex === line.index && 'bg-accent/40',
			)}
			data-testid="text-line"
		>
			<span
				class="text-muted-foreground mt-2 w-7 shrink-0 text-right text-xs tabular-nums select-none"
				title={line.label ? `Line ${n + 1}: ${line.label}` : `Line ${n + 1}`}>{n + 1}</span
			>
			<div class="min-w-0 flex-1">
				{#if fixing?.key === lineKey(line)}
					{@const draft = fixing}
					<div class="flex flex-col gap-1 py-1" data-testid="text-line-editor">
						<Textarea
							class={large ? 'text-base' : 'text-sm'}
							aria-label={`Correct line ${n + 1}`}
							value={draft.text}
							oninput={(e) => (fixing = { key: draft.key, text: e.currentTarget.value })}
							onkeydown={(e) => {
								e.stopPropagation();
								if (e.key === 'Escape') fixing = null;
								else if (e.key === 'Enter' && !e.shiftKey) {
									e.preventDefault();
									correct(line, draft.text);
								} else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
									// Saving with a correction open saves it: the correction is applied first.
									e.preventDefault();
									correct(line, draft.text);
									void controller.save();
								}
							}}
						/>
						<div class="flex gap-1">
							<Button size="xs" onclick={() => correct(line, draft.text)}>
								<Check class="size-3.5" /> Apply
							</Button>
							<Button size="xs" variant="ghost" onclick={() => (fixing = null)}>
								<X class="size-3.5" /> Cancel
							</Button>
							<span class="text-muted-foreground self-center text-xs"
								>Enter applies (Shift+Enter for a new line), Escape cancels. The spans in it follow
								the words they marked; a span whose words are gone is removed.</span
							>
						</div>
					</div>
				{:else}
					<p
						class={cn(
							'break-words whitespace-pre-wrap',
							large ? 'text-base' : 'text-sm',
							editing ? 'cursor-text' : '',
						)}
						style:line-height={`${(large ? 30 : 24) + depth * LANE}px`}
						data-line={n}
						data-testid={`${testid}-line-${n}`}
						onpointerup={(e) => onpointerup(e, line)}
					>{#each segmentText(line.text, marks) as seg (seg.start)}{@const ghostCovering = ghostMarks.filter(
								(g) => g.start <= seg.start && g.end >= seg.end,
							)}{@const style = stretchStyle(
								seg.covering,
								ghostCovering,
								lanes,
								ghostLanes,
								laneCount,
							)}{#each marks.filter((m) => m.index === controller.selectedIndex && m.start === seg.start) as m (m.index)}{@render handle(
									line,
									m,
									'start',
								)}{/each}<span
								data-offset={seg.start}
								class={cn(seg.covering.length > 0 && 'cursor-pointer')}
								style:background-image={style.image || undefined}
								style:background-size={style.size || undefined}
								style:background-position={style.position || undefined}
								style:background-repeat="no-repeat"
								style:padding-bottom={seg.covering.length + ghostCovering.length > 0
									? `${depth * LANE}px`
									: undefined}
								title={[
									...seg.covering.map((m) => m.label || 'no class'),
									...ghostCovering.map((g) => `${g.label || 'no class'} (compared)`),
								].join(' · ') || undefined}
								data-testid={seg.covering.length > 0 ? 'span-stretch' : undefined}>{seg.text}</span
							>{#each marks.filter((m) => m.index === controller.selectedIndex && m.end === seg.end) as m (m.index)}{@render handle(
									line,
									m,
									'end',
								)}{/each}{#each ends.get(seg.end) ?? [] as m (m.index)}<span
									class="ml-0.5 rounded px-1 align-super text-[0.65rem] font-medium select-none"
									style:color={colour(m.label)}
									style:outline={`1px solid ${colour(m.label)}`}
									data-span-end={m.index}
									data-testid="span-tag"
									aria-hidden="true">{m.label || '?'}</span
								>{/each}{/each}</p>
				{/if}
			</div>
			{#if editing && fixing?.key !== lineKey(line)}
				<Button
					variant="ghost"
					size="icon-xs"
					class="mt-1 opacity-60 group-hover:opacity-100"
					aria-label={`Correct the text of line ${n + 1}`}
					title="Correct this line's text"
					onclick={() => (fixing = { key: lineKey(line), text: line.text })}
				>
					<Pencil class="size-3.5" />
				</Button>
			{/if}
		</div>
	{/each}
</div>
