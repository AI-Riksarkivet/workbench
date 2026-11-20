<script lang="ts">
	// The class picker, opened at a region just drawn or asked to be relabelled: the task's classes that region may take,
	// each with its number key and colour. Keyboard first: a digit picks its class, typing narrows the list, the arrows
	// move through it and Enter picks; it opens on the class picked last. Escape closes it, which for a region just drawn
	// on a task with closed classes undoes the drawing (`controller.dismissPicker`). A press outside picks nothing and
	// closes it the same way.
	import { cn } from '@riksarkivet/ui/utils';
	import type { AnnotatorController } from './controller.svelte.ts';

	let {
		controller,
		anchor,
		onclose,
	}: {
		controller: AnnotatorController;
		/** Where the picker opens, in CSS pixels within its positioned container: by the region's corner. */
		anchor: { x: number; y: number; width: number; height: number };
		/** After it closes, with or without a class: focus goes back to what was being labelled. */
		onclose?: () => void;
	} = $props();

	const open = $derived(controller.picker);
	const offered = $derived(open ? controller.classesForRow(open.index) : []);
	const shown = $derived(controller.pickerShown);
	const highlight = $derived(controller.pickerHighlight);
	let input = $state<HTMLInputElement | null>(null);

	const current = $derived(
		open ? (controller.rows.find((r) => r.index === open.index)?.label ?? '') : '',
	);

	// The field takes the focus once it is drawn, so typing goes to it; a key that arrives before is the controller's
	// (`pickerKey`) all the same.
	$effect(() => {
		if (open && input) input.focus();
	});

	function pick(name: string): void {
		controller.pickClass(name);
		onclose?.();
	}

	function onkeydown(e: KeyboardEvent): void {
		e.stopPropagation();
		// Typing edits the field itself; every other key is the picker's.
		if (e.key.length === 1 && !/^[1-9]$/.test(e.key)) return;
		if (e.key === 'Backspace') return;
		if (/^[1-9]$/.test(e.key) && controller.pickerQuery !== '') return;
		if (!controller.pickerKey(e.key)) return;
		e.preventDefault();
		if (!controller.picker) onclose?.();
	}

	// Placed beside the region, kept inside its container: to the region's right when there is room, else its left.
	let box = $state<HTMLDivElement | null>(null);
	const WIDTH = 224;
	const left = $derived.by(() => {
		const host = box?.parentElement?.clientWidth ?? 0;
		const right = anchor.x + anchor.width + 8;
		if (host === 0 || right + WIDTH <= host) return right;
		return Math.max(4, anchor.x - WIDTH - 8);
	});
	const top = $derived.by(() => {
		const host = box?.parentElement?.clientHeight ?? 0;
		const height = box?.offsetHeight ?? 0;
		if (host === 0) return anchor.y;
		return Math.max(4, Math.min(anchor.y, host - height - 4));
	});
</script>

{#if open && offered.length > 0}
	<!-- A press anywhere else on the canvas closes the picker without a class. -->
	<button
		type="button"
		class="absolute inset-0 z-20 cursor-default"
		aria-label="Close the class picker"
		tabindex="-1"
		onclick={() => {
			controller.dismissPicker();
			onclose?.();
		}}
	></button>
	<div
		bind:this={box}
		class="bg-popover text-popover-foreground absolute z-30 flex w-56 flex-col overflow-hidden rounded-lg border shadow-lg"
		style:left={`${left}px`}
		style:top={`${top}px`}
		role="dialog"
		aria-label="Pick a class"
		data-testid="class-picker"
	>
		<div class="border-b p-1.5">
			<input
				bind:this={input}
				bind:value={controller.pickerQuery}
				class="bg-background focus-visible:ring-ring h-7 w-full rounded-md border px-2 text-sm outline-none focus-visible:ring-2"
				placeholder="Class: type, or press 1–9"
				aria-label="Filter the classes"
				aria-controls="class-picker-list"
				aria-activedescendant={shown[highlight] ? `class-option-${highlight}` : undefined}
				role="combobox"
				aria-expanded="true"
				oninput={() => (controller.pickerHighlight = 0)}
				{onkeydown}
			/>
		</div>
		<ul id="class-picker-list" class="max-h-64 overflow-y-auto p-1" role="listbox">
			{#each shown as c, i (c.name)}
				{@const key = offered.indexOf(c) + 1}
				<li
					id={`class-option-${i}`}
					role="option"
					aria-selected={i === highlight}
					class={cn(
						'flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm',
						i === highlight ? 'bg-accent text-accent-foreground' : 'hover:bg-muted',
					)}
					onpointerenter={() => (controller.pickerHighlight = i)}
					onpointerdown={(e) => {
						e.preventDefault();
						pick(c.name);
					}}
				>
					<span
						class="size-3 shrink-0 rounded-sm"
						style:background-color={controller.classColour.get(c.name) ?? 'var(--muted-foreground)'}
					></span>
					<span class="min-w-0 flex-1 truncate">{c.name}</span>
					{#if c.name === current}<span class="text-muted-foreground text-xs">current</span>{/if}
					{#if key <= 9}<kbd class="text-muted-foreground font-mono text-xs">{key}</kbd>{/if}
				</li>
			{:else}
				<li class="text-muted-foreground px-2 py-1.5 text-sm">
					No class matches “{controller.pickerQuery}”.
				</li>
			{/each}
		</ul>
		<p class="text-muted-foreground border-t px-2 py-1 text-[0.7rem]">
			Enter picks · Esc {open.reason === 'drawn' && !controller.allowsUnlabelled
				? `discards the ${controller.rows.find((r) => r.index === open.index)?.shape === 'text' ? 'span' : 'region'}`
				: 'leaves it as it is'}
		</p>
	</div>
{/if}
