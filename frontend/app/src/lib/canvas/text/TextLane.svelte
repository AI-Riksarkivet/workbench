<script lang="ts">
	// The item's text under its picture or its recording: every row carrying text (a transcribed region of a page, a
	// recording's transcribed segment) as a line in reading order, each a span-labeling surface (`TextLines`). Shown when
	// the task has classes that mark text and a row holds text; collapsible, so the picture keeps its room.
	import { ChevronDown, ChevronUp } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import ClassPicker from '../ClassPicker.svelte';
	import type { AnnotatorController } from '../controller.svelte.ts';
	import TextLines from './TextLines.svelte';
	import type { TextLine } from './spans.ts';

	let { controller, lines }: { controller: AnnotatorController; lines: readonly TextLine[] } =
		$props();

	let collapsed = $state(false);
	let host = $state<HTMLDivElement | null>(null);
	let anchor = $state<{ x: number; y: number; width: number; height: number } | null>(null);
	$effect(() => {
		const open = controller.picker;
		void controller.rows;
		if (!open || !host) {
			anchor = null;
			return;
		}
		const tag = host.querySelector<HTMLElement>(`[data-span-end="${open.index}"]`);
		if (!tag) {
			anchor = null;
			return;
		}
		const at = tag.getBoundingClientRect();
		const origin = host.getBoundingClientRect();
		anchor = {
			x: at.left - origin.left,
			y: at.top - origin.top + host.scrollTop,
			width: at.width,
			height: at.height,
		};
	});
</script>

<section
	class="border-border bg-card flex max-h-[40%] min-h-0 shrink-0 flex-col border-t"
	aria-label="The item's text"
	data-testid="text-lane"
>
	<div class="flex items-center justify-between px-3 py-1">
		<span class="text-muted-foreground text-xs font-medium"
			>Text · {lines.length} line{lines.length === 1 ? '' : 's'} · select a stretch to mark it</span
		>
		<Button
			variant="ghost"
			size="icon-xs"
			aria-label={collapsed ? 'Show the text' : 'Hide the text'}
			aria-expanded={!collapsed}
			onclick={() => (collapsed = !collapsed)}
		>
			{#if collapsed}<ChevronUp class="size-3.5" />{:else}<ChevronDown class="size-3.5" />{/if}
		</Button>
	</div>
	{#if !collapsed}
		<div class="relative min-h-0 overflow-y-auto px-2 pb-2" bind:this={host}>
			<TextLines {controller} {lines} testid="text-lane-lines" />
			{#if anchor}
				<ClassPicker {controller} {anchor} />
			{/if}
		</div>
	{/if}
</section>
