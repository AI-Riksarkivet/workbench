<script lang="ts">
	// The tool bar — the canvas's command surface. Fully controlled: it reads and writes the controller, never the
	// engine directly. Every control is a button reached by Tab with its focus ring, and each tool names its hotkey
	// (`aria-keyshortcuts`), so a tool is chosen from the keyboard either way.
	import {
		ArrowLeft,
		Keyboard,
		Eye,
		Pencil,
		Trash2,
		Spline,
		Eraser,
		Undo2,
		Redo2,
		Save,
		SlidersHorizontal,
	} from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import * as Popover from '@riksarkivet/ui/popover';
	import { Slider } from '@riksarkivet/ui/slider';
	import { cn } from '@riksarkivet/ui/utils';
	import { COMMITS_SHAPE } from '@workbench/labeling/shape-types';
	import { TOOL_DEFS, bandsOf } from './tool-defs.ts';
	import AssistPanel from './AssistPanel.svelte';
	import { TIME_LETTERS } from './temporal/time-keys.ts';
	import type { AnnotatorController } from './controller.svelte.ts';

	let {
		controller,
		exitHref,
		narrow = false,
		unreadable = false,
		onhelp,
		kind = null,
	}: {
		controller: AnnotatorController;
		/** The way back: the project's queue. */
		exitHref: string;
		/** A narrow page: the bar wraps onto more rows rather than scrolling sideways (WCAG 2.2 1.4.10). */
		narrow?: boolean;
		/** The item's annotations could not be read: until then a canvas with no table is still loading. */
		unreadable?: boolean;
		/** Opens the list of the canvas's keys. */
		onhelp: () => void;
		/** What the item is, for the assist panel; null where no unit is labelled (no assist). */
		kind?: 'image' | 'audio' | 'video' | 'text' | null;
	} = $props();

	const editing = $derived(controller.readOnly === null);

	/** A tool's key, or null where the item has time and that letter is the time's (`time-keys.ts`). */
	function keyOf(key: string): string | null {
		return controller.timeKeys !== null && TIME_LETTERS.has(key.toLowerCase()) ? null : key;
	}

	// A DRAWING tool is offered only when the open task's ontology permits the shape it commits, so an annotator never
	// draws with a tool the task would refuse at submit. `allowedTools` is null when unconstrained. Select, pan and lasso
	// navigate rather than commit, so no task restriction hides them.
	const visible = $derived(
		TOOL_DEFS.filter(
			(t) =>
				(!t.drawing || controller.canDraw) &&
				(!t.cv || controller.cvCapable) &&
				(!COMMITS_SHAPE.has(t.tool) ||
					controller.allowedTools === null ||
					controller.allowedTools.has(t.tool)),
		),
	);

	/** The bar in BANDS: navigate · draw · assist, in reading order. Empty bands are dropped, so a separator never
	 *  stands beside nothing. */
	const bands = $derived(bandsOf(visible));
	/** A text item has no picture: no drawing tool, adjustment or polygon applies to it. */
	const pictured = $derived(kind !== 'text');
</script>

<!-- `overflow-x-auto` because a wide ontology can outrun a narrow canvas, and a bar that WRAPS moves the canvas down
     a row as tools appear. -->
<div
	class={cn(
		'border-sidebar-border bg-sidebar flex w-full shrink-0 items-center gap-1 border-b px-2',
		narrow ? 'min-h-11 flex-wrap py-1' : 'h-11 overflow-x-auto',
	)}
	role="toolbar"
	aria-label="Labeling tools"
	data-testid="annotator-toolbar"
>
	<Button variant="ghost" size="icon-sm" href={exitHref} aria-label="Back to the project's queue">
		<ArrowLeft class="size-4" />
	</Button>
	<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>

	<Button
		variant="ghost"
		size="icon-sm"
		aria-label="Canvas keys"
		aria-keyshortcuts="?"
		title="Canvas keys (?)"
		onclick={onhelp}
	>
		<Keyboard class="size-4" />
	</Button>
	{#if editing}
		<Button
			variant={controller.mode === 'edit' ? 'default' : 'ghost'}
			size="icon-sm"
			aria-label="Edit mode"
			title={controller.mode === 'edit' ? 'Edit mode (click to view)' : 'View mode (click to edit)'}
			aria-pressed={controller.mode === 'edit'}
			onclick={() => controller.toggleMode()}
		>
			{#if controller.mode === 'edit'}<Pencil class="size-4" />{:else}<Eye class="size-4" />{/if}
		</Button>
	{/if}

	<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>

	{#each pictured ? bands : [] as band, i (band.group)}
		{#if i > 0}
			<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0" data-testid="tool-band-sep"></div>
		{/if}
		{#each band.tools as t (t.tool)}
			{@const Icon = t.icon}
			{@const cvLoading =
				t.cv === true && controller.activeTool === t.tool && !controller.cvReady.has(t.tool)}
			<Button
				variant={controller.activeTool === t.tool ? 'default' : 'ghost'}
				size="icon-sm"
				aria-label={t.label}
				aria-keyshortcuts={keyOf(t.key) ?? undefined}
				title={`${t.label}${keyOf(t.key) ? ` (${t.key})` : ''}${cvLoading ? ' — detecting corners…' : ''}`}
				aria-pressed={controller.activeTool === t.tool}
				disabled={!controller.canDraw && COMMITS_SHAPE.has(t.tool)}
				data-cvready={t.cv ? controller.cvReady.has(t.tool) : undefined}
				data-snapped={t.tool === 'magnetic' ? controller.magneticSnapped : undefined}
				onclick={() => controller.setTool(t.tool)}
			>
				<Icon class={cn('size-4', cvLoading && 'animate-pulse')} />
			</Button>
		{/each}
	{/each}

	{#if controller.activeTool === 'brush'}
		<Button
			variant={controller.brushOptions.erasing ? 'default' : 'ghost'}
			size="icon-sm"
			aria-label="Erase"
			title="Erase (brush)"
			aria-pressed={controller.brushOptions.erasing}
			onclick={() => controller.setBrushOptions({ erasing: !controller.brushOptions.erasing })}
		>
			<Eraser class="size-4" />
		</Button>
		<div class="flex shrink-0 items-center gap-1.5 px-1" data-testid="brush-controls">
			<Slider
				class="w-24"
				min={4}
				max={80}
				step={2}
				value={controller.brushOptions.radius}
				aria-label="Brush size"
				onValueChange={(radius) => controller.setBrushOptions({ radius })}
			/>
			<span class="text-muted-foreground w-6 text-right text-[10px] tabular-nums">
				{controller.brushOptions.radius}
			</span>
		</div>
		<Button
			variant="ghost"
			size="sm"
			class="shrink-0 px-2 text-[10px] font-medium tracking-wide uppercase"
			title={controller.brushOptions.output === 'mask'
				? 'Commits a raster mask (click to commit a traced polygon instead)'
				: 'Commits a traced polygon (click to commit a raster mask instead)'}
			data-testid="brush-output"
			onclick={() =>
				controller.setBrushOptions({
					output: controller.brushOptions.output === 'mask' ? 'polygon' : 'mask',
				})}
		>
			{controller.brushOptions.output === 'mask' ? 'Mask' : 'Poly'}
		</Button>
	{/if}

	<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>
	<!-- Display-only adjustments for the page image: they theme the view, never the data. -->
	{#if pictured}
		<Popover.Root>
			<Popover.Trigger>
				{#snippet child({ props })}
					<Button
						{...props}
						variant={controller.imageAdjusted ? 'default' : 'ghost'}
						size="icon-sm"
						aria-label="Image adjustments"
						title={controller.imageAdjusted
							? 'Image adjustments (active — not saved with annotations)'
							: 'Image adjustments (brightness · contrast · saturation)'}
						data-testid="image-adjust-trigger"
					>
						<SlidersHorizontal class="size-4" />
					</Button>
				{/snippet}
			</Popover.Trigger>
			<Popover.Content class="w-64 p-3" data-testid="image-adjust">
				<div class="flex flex-col gap-3">
					{#each [['brightness', 'Brightness'], ['contrast', 'Contrast'], ['saturation', 'Saturation']] as const as [key, label] (key)}
						<div class="flex items-center gap-2">
							<span class="text-muted-foreground w-20 shrink-0 text-xs">{label}</span>
							<Slider
								class="flex-1"
								min={0.2}
								max={2}
								step={0.05}
								value={controller.imageAdjust[key]}
								aria-label={label}
								onValueChange={(value) => controller.setImageAdjust({ [key]: value })}
							/>
							<span class="text-muted-foreground w-8 shrink-0 text-right text-[10px] tabular-nums">
								{controller.imageAdjust[key].toFixed(2)}
							</span>
						</div>
					{/each}
					<Button
						variant="ghost"
						size="sm"
						class="self-end"
						disabled={!controller.imageAdjusted}
						data-testid="image-adjust-reset"
						onclick={() => controller.resetImageAdjust()}
					>
						Reset
					</Button>
				</div>
			</Popover.Content>
		</Popover.Root>
	{/if}

	{#if unreadable}
		<!-- Said where the tools are: the annotations could not be read, so nothing drawn here could be saved. -->
		<span
			class="text-destructive ml-2 shrink-0 text-xs font-medium"
			data-testid="canvas-readonly"
			title="The annotations for this item could not be read, so editing is disabled — saving would overwrite work that was never loaded."
		>
			read-only — annotations unavailable
		</span>
	{/if}

	{#if editing && kind !== null}
		<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>
		<AssistPanel {controller} {kind} />
	{/if}

	{#if editing}
		<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>

		<Button
			variant="ghost"
			size="icon-sm"
			aria-label="Undo"
			aria-keyshortcuts="Control+Z"
			title="Undo (Ctrl+Z)"
			disabled={!controller.canUndo}
			onclick={() => controller.undo()}
		>
			<Undo2 class="size-4" />
		</Button>
		<Button
			variant="ghost"
			size="icon-sm"
			aria-label="Redo"
			aria-keyshortcuts="Control+Shift+Z Control+Y"
			title="Redo (Ctrl+Shift+Z or Ctrl+Y)"
			disabled={!controller.canRedo}
			onclick={() => controller.redo()}
		>
			<Redo2 class="size-4" />
		</Button>
		<Button
			variant={controller.canSave ? 'default' : 'ghost'}
			size="icon-sm"
			aria-label="Save"
			aria-keyshortcuts="Control+S"
			title={controller.saveError ?? (controller.dirty ? 'Save (Ctrl+S)' : 'No unsaved edits')}
			disabled={!controller.canSave}
			onclick={() => controller.save()}
		>
			<Save
				class={cn(
					'size-4',
					controller.saving && 'animate-pulse',
					controller.saveError && 'text-destructive',
				)}
			/>
		</Button>

		<div class="bg-sidebar-border mx-1 h-6 w-px shrink-0"></div>

		{#if pictured}
			<Button
				variant="ghost"
				size="icon-sm"
				aria-label="Convert to polygon"
				aria-keyshortcuts="P"
				title={controller.convertible
					? 'Convert to polygon (P)'
					: 'Convert to polygon (P): select a box first'}
				disabled={!controller.convertible}
				onclick={() => controller.convertToPolygon()}
			>
				<Spline class="size-4" />
			</Button>
		{/if}
		<Button
			variant="ghost"
			size="icon-sm"
			aria-label="Delete"
			aria-keyshortcuts="Delete"
			title="Delete the selection (Del)"
			disabled={controller.selectedIndex == null}
			onclick={() => controller.deleteSelected()}
		>
			<Trash2 class="size-4" />
		</Button>
	{/if}

	<div class="ml-auto flex items-center gap-1.5 pl-2">
		<span
			class={cn('size-2 rounded-full', controller.dirty ? 'bg-warning' : 'bg-transparent')}
			title={controller.dirty ? 'Unsaved edits' : 'No pending edits'}
		></span>
		<span class="text-muted-foreground text-[10px] tabular-nums" title="Shapes on this item">
			{controller.shapes.length}
		</span>
	</div>
</div>
