<script lang="ts">
	/**
	 * The labeling canvas over a task's unit: the tool bar above, the claimed items down the left, the image with its
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
	import { Button } from '@workbench/ui/button';
	import { Badge } from '@workbench/ui/badge';
	import { Dialog } from '@workbench/ui/dialog';
	import { ResizableSplit } from '@workbench/ui/resizable-split';
	import { cn } from '@workbench/ui/utils';
	import { fetchDraft } from '#lib/api/projects.api.ts';
	import { login } from '#lib/auth.svelte.ts';
	import type { TaskDetail } from '#lib/projects/types.ts';
	import { viewTransitions } from '#lib/view-transition.svelte.ts';
	import { AnnotatorController } from './controller.svelte.ts';
	import AnnotationSidebar from './AnnotationSidebar.svelte';
	import CanvasToolbar from './CanvasToolbar.svelte';
	import ClassificationBar from './ClassificationBar.svelte';
	import Filmstrip from './Filmstrip.svelte';
	import ImageViewer from './ImageViewer.svelte';
	import TaskStreamNav from './TaskStreamNav.svelte';
	import ZoomControls from '#lib/media/image/ZoomControls.svelte';
	import { dropPendingEdits, isEmpty, keepPendingEdits, keptPendingEdits } from './pending.ts';
	import { canvasSettings } from './settings.ts';
	import type { StreamPosition } from './task-stream.ts';
	import { TOOL_DEFS, TOOL_KEYS, gestureOf, isCvTool } from './tool-defs.ts';

	let {
		task,
		unitKey,
		imageUrl,
		annotationsUrl,
		readOnly,
		person,
		stream,
		claimed,
		exitHref,
		link,
	}: {
		task: TaskDetail;
		/** The open unit's key path. */
		unitKey: string;
		/** The unit's picture, a signed link. */
		imageUrl: string;
		/** The unit's annotations door, its project, task and dataset on it. */
		annotationsUrl: string;
		/** Why this reader may not edit the item, or null when they may. */
		readOnly: string | null;
		/** Who is signed in, whose unsaved edits this tab keeps across a sign-in. */
		person: string;
		stream: StreamPosition;
		/** The reader's claimed items in the project, for the filmstrip. */
		claimed: TaskDetail[];
		exitHref: string;
		link: (path: string) => string;
	} = $props();

	const controller = new AnnotatorController();
	controller.autosaveIdleMs = canvasSettings().autosaveIdleSeconds * 1000;
	// The task's captured ontology, read once with the task: the rail offers no tool the task will refuse, the inspector
	// its relations, tag choices, attributes and transcription per class; the submit still enforces it. Read untracked,
	// once: the page remounts this shell per task.
	controller.configure(
		untrack(() => task.ontology),
		untrack(() => task.task_id),
	);
	controller.setReadOnly(untrack(() => readOnly));
	const keptUnder = untrack(() => `${task.task_id}/${unitKey}`);

	onMount(() => {
		// Links live on the task's draft, not in the annotations table: read back once at open, so the next draft save
		// carries them rather than an empty list. `restoreLinks` keeps links drawn before the read answered.
		let alive = true;
		void fetchDraft(task.task_id).then((draft) => {
			if (alive && draft.ok && draft.data !== null && draft.data.links.length > 0)
				controller.restoreLinks(draft.data.links);
		});
		// Autosave, run from here because a component has a lifecycle and the controller does not: the timer dies with
		// the canvas. A reader who may not edit gets none.
		if (controller.readOnly === null) controller.startAutosave();
		return () => {
			alive = false;
			controller.stopAutosave();
		};
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

	function signInAgain(): void {
		keepPendingEdits(person, keptUnder, controller.pendingEdits());
		// The edits are kept for after the sign-in, so leaving for the issuer asks nothing.
		leaving = true;
		void login(page.url.pathname + page.url.search);
	}

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

	let width = $state(1440);
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

	const keyHelp = $derived(
		gestureOf(controller.activeTool) === 'view'
			? 'Arrow keys move the view. Plus, minus and 0 zoom.'
			: gestureOf(controller.activeTool) === 'drag'
				? 'Arrow keys move the cursor; Space starts the stroke, the arrows draw it, Space ends it. Escape cancels.'
				: 'Arrow keys move the cursor; Space presses there. Enter finishes a polygon, Escape cancels.',
	);

	function onKeydown(e: KeyboardEvent): void {
		const el = e.target as HTMLElement | null;
		if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
		const k = e.key.toLowerCase();
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
	<div class="flex h-full w-full flex-col">
		<ClassificationBar {controller} />
		<div class="relative min-h-0 w-full flex-1">
			<!-- The canvas region: focusable, named, and described by what its keys do. The shapes on it are also reached
			     as rows of the review list beside it. `application` is the WAI-ARIA role of a widget that takes its own
			     keys, which Svelte's a11y check counts as non-interactive. -->
			<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
			<div
				class="focus-visible:ring-ring absolute inset-0 outline-none focus-visible:ring-2 focus-visible:ring-inset"
				role="application"
				aria-label={`Labeling canvas: ${unitKey}`}
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
					{imageUrl}
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
			{#if refusal}
				<div
					class="bg-background/95 absolute inset-0 z-20 flex flex-col items-center justify-center gap-1 p-6 text-center text-sm"
					data-testid="canvas-refusal"
				>
					<p class="font-medium">This item cannot be opened on the canvas.</p>
					<p class="text-muted-foreground break-words">{refusal}</p>
				</div>
			{/if}
			<Badge
				variant={status === 'failed' ? 'destructive' : 'secondary'}
				class="pointer-events-none absolute bottom-2 left-2 z-10 max-w-[calc(100%-1rem)] truncate font-mono shadow-sm backdrop-blur"
				data-testid="annotate-status"
			>
				{statusText}{controller.saveStatus ? ` · ${controller.saveStatus}` : ''}
			</Badge>
			<TaskStreamNav {stream} />
			<ZoomControls target={controller} class={narrow ? 'bottom-10' : undefined} />
		</div>
	</div>
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
	/>

	{#if narrow}
		<!-- Stacked: the image above, the inspector under it, the page scrolling in one direction (WCAG 2.2 1.4.10). -->
		<div class="flex min-h-0 flex-1 flex-col overflow-y-auto">
			<div class="h-[60vh] min-h-64 shrink-0">{@render viewer()}</div>
			<div class="border-t">
				<AnnotationSidebar {controller} />
			</div>
		</div>
	{:else}
		<div class="flex min-h-0 flex-1">
			<Filmstrip items={claimed} activeTaskId={task.task_id} projectId={task.project_id} {link} />
			<div class="min-w-0 flex-1">
				<ResizableSplit storageKey="workbench-canvas" initial={0.72} minLeft={420} minRight={320}>
					{#snippet left()}
						{@render viewer()}
					{/snippet}
					{#snippet right()}
						<AnnotationSidebar {controller} />
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
		{#if controller.signedOut}
			<Button variant="outline" size="xs" onclick={signInAgain}>Sign in again</Button>
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
				<dt class="font-mono">{t.key}</dt>
				<dd>{t.label}</dd>
			{/each}
			<dt class="font-mono">Del</dt>
			<dd>Delete the selected annotation</dd>
			<dt class="font-mono">A · R</dt>
			<dd>Accept · reject the selected annotation, and go to the next</dd>
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
