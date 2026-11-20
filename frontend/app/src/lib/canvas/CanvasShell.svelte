<script lang="ts">
	/**
	 * The labeling canvas over a task's unit: the tool bar above, the items it walks down the left, the image with its
	 * overlays in the middle and the review inspector on the right; below 740 px the inspector goes under the image and
	 * the items collapse into the queue's arrows. It owns the controller and the keyboard; every child is controlled. The
	 * page remounts it per task (`{#key}`), so each item loads fresh.
	 *
	 * KEYBOARD. A tool is chosen by its hotkey or its button, and `?` lists every key. The canvas itself is one focusable
	 * region with a name: on it the arrow keys move a cursor over the image (Shift for ten times the step) and Space
	 * presses there, through the engine's own pointer path (`InteractionManager.pointerAt`). A `click` tool (select, point,
	 * a polygon's vertex) takes one Space; a `drag` tool (a box, a line, a stroke) holds the press from one Space to the
	 * next while the arrows draw it. With the pan tool the arrows move the view; `+`, `-` and `0` zoom. Enter, Escape and
	 * Backspace stay the drawing tool's (commit, cancel, undo a vertex), and every other review key works anywhere.
	 *
	 * UNSAVED WORK. Leaving the item saves it first, and stays when that save fails; closing the tab while edits are
	 * unsaved asks first. A save refused because the session ended keeps the edits in this tab (`pending.ts`) and offers
	 * to sign in again, and they are put back on the item when it opens after the sign-in.
	 */
	import { onMount, untrack } from 'svelte';
	import { beforeNavigate, goto } from '$app/navigation';
	import { page } from '$app/state';
	import { Button } from '@riksarkivet/ui/button';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Dialog } from '@riksarkivet/ui/dialog';
	import { ResizableSplit } from '@riksarkivet/ui/resizable-split';
	import { cn } from '@riksarkivet/ui/utils';
	import { beforeSigningInAgain } from '#lib/reauth.svelte.ts';
	import type { TaskDetail } from '#lib/projects/types.ts';
	import { viewTransitions } from '#lib/view-transition.svelte.ts';
	import { AnnotatorController } from './controller.svelte.ts';
	import AnnotationSidebar from './AnnotationSidebar.svelte';
	import CanvasToolbar from './CanvasToolbar.svelte';
	import ClassificationBar from './ClassificationBar.svelte';
	import ClassPicker from './ClassPicker.svelte';
	import TaskPanel from './TaskPanel.svelte';
	import { REVIEW_KEYS } from './task-panel.ts';
	import TemporalCanvas from './temporal/TemporalCanvas.svelte';
	import TextCanvas from './text/TextCanvas.svelte';
	import TextLane from './text/TextLane.svelte';
	import { textLines } from './text/spans.ts';
	import ComparePanel from './compare/ComparePanel.svelte';
	import type { CanvasMedia } from './media.ts';
	import Filmstrip from './Filmstrip.svelte';
	import ImageViewer from './ImageViewer.svelte';
	import TaskStreamNav from './TaskStreamNav.svelte';
	import ZoomControls from '#lib/media/image/ZoomControls.svelte';
	import { dropPendingEdits, isEmpty, keepPendingEdits, keptPendingEdits } from './pending.ts';
	import { canvasSettings } from './settings.ts';
	import type { StreamPosition } from './task-stream.ts';
	import { TOOL_DEFS, TOOL_KEYS, gestureOf, isCvTool } from './tool-defs.ts';
	import { TIME_KEYS, TIME_LETTERS } from './temporal/time-keys.ts';

	let {
		task,
		unitKey,
		media,
		annotationsUrl,
		readOnly,
		person,
		stream,
		walk,
		exitHref,
		link,
		permissions,
		me,
		ontaskchange,
		itemName,
		replicas = [],
	}: {
		task: TaskDetail;
		/** The open unit's key path. */
		unitKey: string;
		/** What is labelled: the unit's picture, or the recording or video its unit is part of, each a signed link. */
		media: CanvasMedia;
		/** The unit's annotations door, its project, task and dataset on it. */
		annotationsUrl: string;
		/** Why this reader may not edit the item, or null when they may. */
		readOnly: string | null;
		/** Who is signed in, whose unsaved edits this tab keeps across a sign-in. */
		person: string;
		stream: StreamPosition;
		/** The items this canvas walks (the reader's claims, or a browse or a review), for the filmstrip: a page of the
		 *  walk around the open item, where it starts in the walk, its length, each item's app path and its name. */
		walk: {
			items: TaskDetail[];
			offset: number;
			total: number;
			hrefOf: (task: TaskDetail, projectId: string) => string;
			label: string;
			stripLabel: string;
			/** A browse takes no transitions: the side panel says what the item is and offers nothing. */
			browsing: boolean;
		};
		exitHref: string;
		link: (path: string) => string;
		/** The reader's permissions on the project: which of the task's transitions the side panel offers. */
		permissions: readonly string[];
		/** Who is signed in, as the annotator names principals; null with sign-in off. */
		me: string | null;
		/** The task after a transition the side panel made. */
		ontaskchange: (task: TaskDetail) => void;
		/** What the item is called (its document and page, or its recording), for the canvas's name. */
		itemName: string;
		/** The item's other replicas (its consensus group), for comparing. */
		replicas?: readonly TaskDetail[];
	} = $props();

	const controller = new AnnotatorController();
	controller.autosaveIdleMs = canvasSettings().autosaveIdleSeconds * 1000;
	// The task's captured ontology, read once with the task: the rail offers no tool the task will refuse, the inspector
	// its relations, tag choices, attributes and transcription per class; the submit still enforces it. Read untracked,
	// once: the page remounts this shell per task.
	controller.configure(untrack(() => task.ontology));
	// Who may edit follows the task: a claim, a submit or a review changes it while the canvas stays open.
	$effect.pre(() => {
		controller.setReadOnly(readOnly);
	});
	const keptUnder = untrack(() => `${task.task_id}/${unitKey}`);
	// The unit the assist doors name.
	controller.unit = untrack(() => ({
		project: task.project_id,
		task: task.task_id,
		dataset: task.source.where ?? '',
		key: unitKey,
	}));
	/** The item's lines of text a span can range into, under a picture or a recording: shown when the task marks text. */
	const lane = $derived(
		media.kind !== 'text' && controller.textSpanClasses.length > 0
			? textLines(controller.rows)
			: [],
	);

	onMount(() => () => controller.stopAutosave());

	// Autosave, run from here because a component has a lifecycle and the controller does not: the timer dies with
	// the canvas. A reader who may not edit gets none, and gets it once the item becomes theirs to edit.
	$effect(() => {
		if (readOnly === null) controller.startAutosave();
		else controller.stopAutosave();
	});

	// Every edit restarts the debounce (`edits`), so the autosave waits from the last one.
	$effect(() => {
		void controller.edits;
		controller.scheduleAutosave();
	});

	// A save that lands drops what a sign-in kept: the edits are on the server now.
	controller.onSaved = () => dropPendingEdits(person, keptUnder);

	function onLoaded(): void {
		const kept = controller.readOnly === null ? keptPendingEdits(person, keptUnder) : null;
		if (kept && !isEmpty(kept)) {
			controller.applyPending(kept);
			controller.notice = {
				tone: 'success',
				text: 'Your unsaved edits are back. Save to keep them.',
			};
		}
	}

	// Signing in again (the shell's one prompt, `#lib/reauth.svelte.ts`) keeps the unsaved edits for after the round
	// trip, so leaving for the issuer asks nothing.
	$effect(() =>
		beforeSigningInAgain(() => {
			keepPendingEdits(person, keptUnder, controller.pendingEdits());
			leaving = true;
		}),
	);

	// Leaving the item saves it first, and a save that fails keeps the person here with the reason on screen. A tab
	// closing (`leave`) cannot wait for a save, so the browser asks instead.
	let leaving = false;
	beforeNavigate((navigation) => {
		if (leaving || !controller.dirty || controller.readOnly !== null) return;
		navigation.cancel();
		if (navigation.type === 'leave' || !navigation.to) return;
		const to = navigation.to.url;
		void (async () => {
			await controller.save();
			if (controller.dirty) {
				controller.notice = {
					tone: 'error',
					text: `${controller.saveError ?? 'The edits were not saved'}: they are still here, so you stayed on this item.`,
				};
				return;
			}
			leaving = true;
			// oxlint-disable-next-line svelte/no-navigation-without-resolve -- the destination of a navigation already under way
			await goto(to);
		})();
	});

	let status = $state<'loading' | 'loaded' | 'failed'>('loading');
	// Ready for a person's input: the picture fitted, the annotations drawn and the controller attached (`loaded`), and
	// no page transition covering the canvas, during which the browser sends every press to the document root
	// (`#lib/view-transition.svelte.ts`). Until then the region says it is busy.
	const ready = $derived(status === 'loaded' && !viewTransitions.running);
	let failure = $state('');
	let refusal = $state<string | null>(null);
	const count = $derived(controller.rows.length);
	const statusText = $derived(
		status === 'loading'
			? 'loading…'
			: status === 'failed'
				? `load failed — ${failure}`
				: `${count} annotation${count === 1 ? '' : 's'}`,
	);

	// Measured by the layout below; until then the window's width, so a narrow page lays the canvas out stacked from its
	// first frame rather than mounting the picture in the split and again under it (measured at 390 px: two stages
	// built and the canvas ready after 3.9 s, against 1.9 s at 1440).
	let width = $state(window.innerWidth);
	/** Below the split's two minimums (420 + 320 px) the inspector goes under the image. */
	const narrow = $derived(width < 740);
	let helpOpen = $state(false);

	// ── the keyboard's cursor, in image space ──
	let cursor = $state<{ x: number; y: number } | null>(null);
	let pressed = $state(false);
	let focused = $state(false);

	const cursorOnScreen = $derived.by(() => {
		void controller.viewport;
		const ctx = controller.ctx;
		if (!cursor || !ctx) return null;
		return ctx.plugins.interaction.worldToScreen(cursor.x, cursor.y);
	});

	function onCanvasFocus(): void {
		focused = true;
		const image = controller.ctx?.plugins.image;
		if (cursor === null && image) cursor = { x: image.imageWidth / 2, y: image.imageHeight / 2 };
	}

	function onCanvasBlur(): void {
		focused = false;
		// A press does not outlive the focus that made it.
		if (pressed && cursor) controller.ctx?.plugins.interaction.pointerAt('up', cursor.x, cursor.y);
		pressed = false;
	}

	const ARROWS: Record<string, [number, number]> = {
		ArrowLeft: [-1, 0],
		ArrowRight: [1, 0],
		ArrowUp: [0, -1],
		ArrowDown: [0, 1],
	};

	function onCanvasKeydown(e: KeyboardEvent): void {
		const ctx = controller.ctx;
		if (!ctx || e.ctrlKey || e.metaKey || e.altKey) return;
		const { image, interaction } = ctx.plugins;
		const arrow = ARROWS[e.key];
		const gesture = gestureOf(controller.activeTool);
		if (arrow) {
			e.preventDefault();
			e.stopPropagation();
			const times = e.shiftKey ? 10 : 1;
			if (gesture === 'view') {
				image.panBy(-arrow[0] * 20 * times, -arrow[1] * 20 * times);
				return;
			}
			const step = Math.max(1, Math.round(Math.max(image.imageWidth, image.imageHeight) / 100));
			const at = cursor ?? { x: image.imageWidth / 2, y: image.imageHeight / 2 };
			const next = {
				x: Math.min(Math.max(at.x + arrow[0] * step * times, 0), image.imageWidth),
				y: Math.min(Math.max(at.y + arrow[1] * step * times, 0), image.imageHeight),
			};
			cursor = next;
			if (pressed) interaction.pointerAt('move', next.x, next.y);
			return;
		}
		if (e.key === ' ') {
			e.preventDefault();
			e.stopPropagation();
			if (!cursor || gesture === 'view') return;
			if (gesture === 'click') {
				interaction.pointerAt('down', cursor.x, cursor.y);
				interaction.pointerAt('up', cursor.x, cursor.y);
			} else if (!pressed) {
				interaction.pointerAt('down', cursor.x, cursor.y);
				pressed = true;
			} else {
				interaction.pointerAt('up', cursor.x, cursor.y);
				pressed = false;
			}
			return;
		}
		if (e.key === 'Escape' && pressed) {
			// Cancel the held gesture: the tool's own Escape drops it, and the press is let go.
			pressed = false;
			return;
		}
		if (e.key === '+' || e.key === '=') {
			e.stopPropagation();
			controller.zoomIn();
		} else if (e.key === '-') {
			e.stopPropagation();
			controller.zoomOut();
		} else if (e.key === '0') {
			e.stopPropagation();
			controller.resetView();
		}
	}

	/** Where the class picker opens: by the region it labels, on the screen as the view is now. */
	const pickerAnchor = $derived(
		controller.picker ? controller.screenBoxOf(controller.picker.index) : null,
	);
	let regionEl = $state<HTMLDivElement | null>(null);
	/** Back to the canvas once the picker closes, so the next key draws or moves rather than going nowhere. */
	function refocus(): void {
		// The image canvas's region, or on a recording or a video the temporal canvas, whose keys play and step.
		(
			regionEl ??
			viewerEl?.querySelector<HTMLElement>(
				'[data-testid="temporal-canvas"], [data-testid="text-canvas"]',
			)
		)?.focus();
	}
	let viewerEl = $state<HTMLDivElement | null>(null);

	const keyHelp = $derived(
		gestureOf(controller.activeTool) === 'view'
			? 'Arrow keys move the view. Plus, minus and 0 zoom.'
			: gestureOf(controller.activeTool) === 'drag'
				? 'Arrow keys move the cursor; Space starts the stroke, the arrows draw it, Space ends it. Escape cancels.'
				: 'Arrow keys move the cursor; Space presses there. Enter finishes a polygon, Escape cancels.',
	);

	function onKeydown(e: KeyboardEvent): void {
		const el = e.target as HTMLElement | null;
		// Escape lets go of text selected in a line too, so no later class key makes a span of it.
		if (e.key === 'Escape') {
			controller.textSelection = null;
			window.getSelection()?.removeAllRanges();
		}
		// Ctrl+S saves from anywhere on the page, a transcription being typed included; every other key in a field is
		// the field's (its own undo, its letters).
		if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
			e.preventDefault();
			void controller.save();
			return;
		}
		if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
		const k = e.key.toLowerCase();
		// Shift+A, Shift+F and Shift+R judge the whole item (the side panel's review keys, `REVIEW_KEYS`), not one annotation.
		if (e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey && REVIEW_KEYS[k]) return;
		if (e.ctrlKey || e.metaKey) {
			if (k === 'z') {
				e.preventDefault();
				if (e.shiftKey) controller.redo();
				else controller.undo();
			} else if (k === 'y') {
				e.preventDefault();
				controller.redo();
			} else if (k === 's') {
				e.preventDefault();
				void controller.save();
			}
			return;
		}
		if (e.key === '?') {
			e.preventDefault();
			helpOpen = true;
			return;
		}
		// An open class picker takes the keys, even one pressed before its field had the focus.
		if (controller.picker) {
			if (controller.pickerKey(e.key)) {
				e.preventDefault();
				if (!controller.picker) refocus();
			}
			return;
		}
		// The digits pick a class: the selection's, or the next region's when nothing is selected; C opens the picker on
		// the selection.
		if (/^[1-9]$/.test(e.key) && controller.classByKey(e.key)) {
			e.preventDefault();
			return;
		}
		if (k === 'c' && controller.selectedIndex !== null) {
			e.preventDefault();
			controller.openPicker(controller.selectedIndex, 'relabel');
			return;
		}
		// A selected text span: Alt+←/→ move its end, Alt+Shift+←/→ its start, a character at a time.
		if (
			e.altKey &&
			(e.key === 'ArrowLeft' || e.key === 'ArrowRight') &&
			controller.nudgeSpan(e.shiftKey ? 'start' : 'end', e.key === 'ArrowLeft' ? -1 : 1)
		) {
			e.preventDefault();
			return;
		}
		// Text selected in a line: Enter makes a span of it and opens the class picker.
		if (e.key === 'Enter' && controller.textSelection !== null && controller.readOnly === null) {
			const chosen = controller.textSelection;
			controller.textSelection = null;
			window.getSelection()?.removeAllRanges();
			e.preventDefault();
			controller.addTextSpan(chosen.line, chosen.start, chosen.end);
			return;
		}
		// An item with time: its keys play, step and edit from wherever the focus is on the page, and a letter it takes is
		// not also a tool's. A focused button keeps its own Space and Enter.
		const pressesButton =
			(e.key === ' ' || e.key === 'Enter') &&
			el !== null &&
			(el.tagName === 'BUTTON' || el.tagName === 'SELECT' || el.tagName === 'A');
		if (controller.timeKeys && !pressesButton && controller.timeKeys(e)) return;
		if (controller.timeKeys && TIME_LETTERS.has(k)) return;
		const tool = TOOL_KEYS[k];
		if (tool) {
			// A drawing hotkey needs a live engine; a phantom tool with no canvas behind it would swallow the review keys.
			const spatialTool = tool !== 'select' && tool !== 'pan';
			if (
				(controller.canDraw || !spatialTool) &&
				(!spatialTool || controller.ctx !== null) &&
				(!isCvTool(tool) || controller.cvCapable)
			) {
				controller.setTool(tool);
			}
			return;
		}
		// While a DRAWING tool is active, Enter, Escape and Backspace are the tool's (commit, cancel, undo a vertex), not
		// the review keys'; a focused button keeps its own Enter.
		const drawingActive =
			controller.ctx !== null &&
			controller.canDraw &&
			controller.activeTool !== 'select' &&
			controller.activeTool !== 'pan';
		const focusable =
			el && (el.tagName === 'BUTTON' || el.tagName === 'SELECT' || el.tagName === 'A');
		if (
			drawingActive &&
			!focusable &&
			(e.key === 'Enter' || e.key === 'Escape' || e.key === 'Backspace')
		) {
			e.preventDefault();
			controller.forwardToolKey(e.key);
			return;
		}
		if (focusable && (e.key === 'Enter' || e.key === ' ')) return;
		if (k === 'a' || e.key === 'Enter') {
			controller.acceptAndAdvance('accepted');
		} else if (k === 'r') {
			controller.acceptAndAdvance('rejected');
		} else if (k === 'j' || e.key === 'ArrowDown') {
			e.preventDefault();
			controller.next();
		} else if (k === 'k' || e.key === 'ArrowUp') {
			e.preventDefault();
			controller.prev();
		} else if (e.key === 'Delete' || e.key === 'Backspace') {
			controller.deleteSelected();
		} else if (k === 'p') {
			controller.convertToPolygon();
		} else if (e.key === 'Escape') {
			controller.select(null);
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

{#snippet viewer()}
	<div class="flex h-full w-full flex-col" bind:this={viewerEl}>
		<ClassificationBar {controller} />
		<div class="relative min-h-0 w-full flex-1">
			{#if media.kind === 'text'}
				<TextCanvas
					{controller}
					{annotationsUrl}
					documentText={media.text}
					{itemName}
					onload={() => {
						status = 'loaded';
						onLoaded();
					}}
					onerror={(message, code) => {
						status = 'failed';
						failure = message;
						if (code === 422) refusal = message;
					}}
				/>
			{:else if media.kind !== 'image'}
				<TemporalCanvas
					{controller}
					kind={media.kind}
					mediaUrl={media.url}
					{annotationsUrl}
					dataset={media.dataset}
					docId={media.docId}
					parts={media.parts}
					turns={media.turns}
					focus={media.focus}
					{itemName}
					onload={() => {
						status = 'loaded';
						onLoaded();
					}}
					onerror={(message, code) => {
						status = 'failed';
						failure = message;
						if (code === 422) refusal = message;
					}}
				/>
			{:else}
				<!-- The canvas region: focusable, named, and described by what its keys do. The shapes on it are also reached
			     as rows of the review list beside it. `application` is the WAI-ARIA role of a widget that takes its own
			     keys, which Svelte's a11y check counts as non-interactive. -->
				<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
				<!-- The focus ring is drawn on a layer over the picture: a ring on the region itself is painted under its
				     canvas, and showed only where the picture left the canvas clear. -->
				<div
					class="after:ring-ring absolute inset-0 outline-none after:pointer-events-none after:absolute after:inset-0 after:z-10 after:content-[''] focus-visible:after:ring-2 focus-visible:after:ring-inset"
					bind:this={regionEl}
					role="application"
					aria-label={`Labeling canvas: ${itemName}`}
					aria-describedby="canvas-keys"
					aria-roledescription="drawing canvas"
					tabindex="0"
					data-testid="canvas-region"
					aria-busy={!ready}
					data-ready={ready ? 'true' : 'false'}
					data-pressed={pressed ? 'true' : 'false'}
					onfocus={onCanvasFocus}
					onblur={onCanvasBlur}
					onkeydown={onCanvasKeydown}
				>
					<ImageViewer
						imageUrl={media.url}
						{annotationsUrl}
						{controller}
						onload={() => {
							status = 'loaded';
							onLoaded();
						}}
						onerror={(message, code) => {
							status = 'failed';
							failure = message;
							if (code === 422) refusal = message;
						}}
					/>
					{#if focused && cursorOnScreen}
						<span
							class={cn(
								'pointer-events-none absolute z-10 size-5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2',
								pressed ? 'border-primary bg-primary/30' : 'border-primary',
							)}
							style:left={`${cursorOnScreen.x}px`}
							style:top={`${cursorOnScreen.y}px`}
							data-testid="canvas-cursor"
						></span>
					{/if}
				</div>
				<p id="canvas-keys" class="sr-only">{keyHelp}</p>
			{/if}
			{#if refusal}
				<div
					class="bg-background/95 absolute inset-0 z-20 flex flex-col items-center justify-center gap-1 p-6 text-center text-sm"
					data-testid="canvas-refusal"
				>
					<p class="font-medium">This item cannot be opened on the canvas.</p>
					<p class="text-muted-foreground break-words">{refusal}</p>
				</div>
			{/if}
			{#if pickerAnchor}
				<ClassPicker {controller} anchor={pickerAnchor} onclose={refocus} />
			{/if}
			<Badge
				hidden={media.kind !== 'image' && status !== 'failed'}
				variant={status === 'failed' ? 'destructive' : 'secondary'}
				class="pointer-events-none absolute bottom-2 left-2 z-10 max-w-[calc(100%-1rem)] truncate font-mono shadow-sm backdrop-blur"
				data-testid="annotate-status"
			>
				{statusText}{controller.saveStatus ? ` · ${controller.saveStatus}` : ''}
			</Badge>
			<TaskStreamNav {stream} label={walk.label} />
			{#if media.kind === 'image'}
				<ZoomControls target={controller} class={narrow ? 'bottom-10' : undefined} />
			{/if}
		</div>
		{#if lane.length > 0}
			<TextLane {controller} lines={lane} />
		{/if}
	</div>
{/snippet}

{#snippet taskPanel()}
	<TaskPanel
		{controller}
		{task}
		{permissions}
		{me}
		browsing={walk.browsing}
		onchanged={ontaskchange}
	/>
	<!-- Another replica's work is offered only to someone who may review the item: the replicas are independent until
	     then (`require_work_readable`). -->
	<ComparePanel {controller} replicas={permissions.includes('can_review') ? replicas : []} />
{/snippet}

<div
	class="flex min-h-0 w-full min-w-0 flex-1 flex-col"
	bind:clientWidth={width}
	data-layout={narrow ? 'stacked' : 'split'}
>
	<CanvasToolbar
		{controller}
		{exitHref}
		{narrow}
		unreadable={status === 'failed'}
		onhelp={() => (helpOpen = true)}
		kind={media.kind}
	/>

	{#if narrow}
		<!-- Stacked: the image above, the inspector under it, the page scrolling in one direction (WCAG 2.2 1.4.10). -->
		<div class="flex min-h-0 flex-1 flex-col overflow-y-auto">
			<div class="h-[60vh] min-h-64 shrink-0">{@render viewer()}</div>
			<div class="border-t">
				<AnnotationSidebar {controller} head={taskPanel} />
			</div>
		</div>
	{:else}
		<div class="flex min-h-0 flex-1">
			<Filmstrip
				items={walk.items}
				activeTaskId={task.task_id}
				projectId={task.project_id}
				{link}
				hrefOf={walk.hrefOf}
				offset={walk.offset}
				total={walk.total}
				label={walk.stripLabel}
			/>
			<div class="min-w-0 flex-1">
				<ResizableSplit storageKey="workbench-canvas" initial={0.72} minLeft={420} minRight={320}>
					{#snippet left()}
						{@render viewer()}
					{/snippet}
					{#snippet right()}
						<AnnotationSidebar {controller} head={taskPanel} />
					{/snippet}
				</ResizableSplit>
			</div>
		</div>
	{/if}
	<!-- The canvas's outcomes (a save, a relabel, a refusal, why it is read-only), said in a live region. -->
	<div
		class={cn(
			'flex min-h-6 shrink-0 flex-wrap items-center gap-2 border-t px-3 py-1 text-xs',
			controller.notice?.tone === 'error' ? 'text-destructive' : 'text-muted-foreground',
		)}
		role="status"
		data-testid="canvas-notice"
	>
		{#if controller.readOnly !== null && controller.notice === null}
			<span data-testid="canvas-read-only">{controller.readOnly}</span>
		{:else}
			{controller.notice?.text ?? ''}
		{/if}
	</div>
</div>

<Dialog.Root bind:open={helpOpen}>
	<Dialog.Content class="sm:max-w-lg" data-testid="canvas-keys-help">
		<Dialog.Title>Canvas keys</Dialog.Title>
		<dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
			<dt class="font-mono">←↑↓→</dt>
			<dd>Move the cursor on the canvas (Shift: ten steps); with Pan, move the view</dd>
			<dt class="font-mono">Space</dt>
			<dd>Press at the cursor: a click tool's click; a drag tool's stroke starts and ends</dd>
			<dt class="font-mono">Enter · Esc · ⌫</dt>
			<dd>Finish a polygon · cancel the stroke · undo its last vertex</dd>
			<dt class="font-mono">+ · − · 0</dt>
			<dd>Zoom in · zoom out · fit the picture</dd>
			{#each TOOL_DEFS as t (t.tool)}
				{#if !(media.kind !== 'image' && TIME_LETTERS.has(t.key.toLowerCase()))}
					<dt class="font-mono">{t.key}</dt>
					<dd>{t.label}</dd>
				{/if}
			{/each}
			{#if media.kind !== 'image'}
				{#each TIME_KEYS.filter((k) => media.kind === 'video' || !k.video) as k (k.keys)}
					<dt class="font-mono">{k.keys}</dt>
					<dd>{k.does}</dd>
				{/each}
			{/if}
			<dt class="font-mono">1 – 9</dt>
			<dd>
				Give the selected region the task's first nine classes (nothing selected: the next region's)
			</dd>
			{#if media.kind === 'text' || lane.length > 0}
				<dt class="font-mono">Select text · Enter</dt>
				<dd>
					Mark the selected text as a span and pick its class; a digit gives it that class at once
				</dd>
				<dt class="font-mono">Alt+← → · Alt+Shift+← →</dt>
				<dd>Move the selected span's end · its start</dd>
				<dt class="font-mono">✎ · Enter · Shift+Enter</dt>
				<dd>Correct a line's text · apply it · a new line in it</dd>
			{/if}
			<dt class="font-mono">C</dt>
			<dd>Open the class picker on the selected region</dd>
			<dt class="font-mono">Del</dt>
			<dd>Delete the selected annotation</dd>
			<dt class="font-mono">A · R</dt>
			<dd>Accept · reject the selected annotation, and go to the next</dd>
			<dt class="font-mono">Shift+A · Shift+F · Shift+R</dt>
			<dd>Reviewing: accept the item · accept it with your fixes · send it back with a note</dd>
			<dt class="font-mono">Page Up · Page Down</dt>
			<dd>The previous · next item of the walk you are on</dd>
			<dt class="font-mono">J · K</dt>
			<dd>Next · previous annotation</dd>
			<dt class="font-mono">Ctrl+Z · Ctrl+Shift+Z</dt>
			<dd>Undo · redo</dd>
			<dt class="font-mono">Ctrl+S</dt>
			<dd>Save</dd>
			<dt class="font-mono">?</dt>
			<dd>This list</dd>
		</dl>
	</Dialog.Content>
</Dialog.Root>
