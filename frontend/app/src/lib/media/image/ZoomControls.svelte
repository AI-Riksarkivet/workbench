<script lang="ts">
	// The zoom cluster that floats over a still (bottom right): zoom out, the scale, zoom in, fit and actual size.
	// It drives whatever view it is handed: the player's viewer or the labeling canvas.
	import { Maximize, ZoomIn, ZoomOut } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { cn } from '@riksarkivet/ui/utils';
	import type { Zoomable } from './zoom.ts';

	let { target, class: className }: { target: Zoomable; class?: string } = $props();

	const percent = $derived(`${Math.round(target.scale * 100)}%`);
</script>

<div
	class={cn(
		'border-border bg-card/90 text-card-foreground pointer-events-auto absolute right-2 bottom-2 z-10 flex items-center gap-0.5 rounded-lg border p-1 shadow-sm backdrop-blur',
		className,
	)}
	role="toolbar"
	aria-label="Zoom"
	data-testid="zoom-controls"
>
	<Button
		variant="ghost"
		size="icon-xs"
		title="Zoom out (−)"
		aria-label="Zoom out"
		onclick={() => target.zoomOut()}
	>
		<ZoomOut class="size-4" />
	</Button>
	<output
		class="text-muted-foreground min-w-11 text-center text-xs tabular-nums"
		aria-label="Zoom level"
		data-testid="zoom-level">{percent}</output
	>
	<Button
		variant="ghost"
		size="icon-xs"
		title="Zoom in (+)"
		aria-label="Zoom in"
		onclick={() => target.zoomIn()}
	>
		<ZoomIn class="size-4" />
	</Button>
	<div class="bg-border mx-0.5 h-4 w-px" aria-hidden="true"></div>
	<Button
		variant="ghost"
		size="icon-xs"
		title="Fit to view (0)"
		aria-label="Fit to view"
		onclick={() => target.resetView()}
	>
		<Maximize class="size-4" />
	</Button>
	<Button
		variant="ghost"
		size="xs"
		class="px-1.5 text-xs tabular-nums"
		title="Actual size, 100% (1)"
		aria-label="Actual size"
		onclick={() => target.actualSize()}
	>
		1:1
	</Button>
</div>
