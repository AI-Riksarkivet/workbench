<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn } from '../../utils/cn.js';

	// The app's mark: a row of square blocks stepping through the accent's ramp (`--block-1` … `--block-5`), so it
	// takes whatever colour the person chose. Decoration: hidden from assistive technology unless it is given a label.
	let {
		blocks = 5,
		size = 'md',
		label,
		class: className,
		...rest
	}: Omit<HTMLAttributes<HTMLSpanElement>, 'children'> & {
		/** How many blocks, up to five. */
		blocks?: 1 | 2 | 3 | 4 | 5;
		size?: 'sm' | 'md' | 'lg';
		/** A name for the mark when it stands alone; without one it is decoration. */
		label?: string;
	} = $props();

	const RAMP = ['bg-block-1', 'bg-block-2', 'bg-block-3', 'bg-block-4', 'bg-block-5'];
	const steps = $derived(RAMP.slice(RAMP.length - blocks));
	const block = $derived({ sm: 'size-1.5', md: 'size-2', lg: 'size-3' }[size]);
</script>

<span
	data-slot="block-mark"
	class={cn('inline-flex shrink-0 items-end gap-px', className)}
	role={label ? 'img' : undefined}
	aria-label={label}
	aria-hidden={label ? undefined : 'true'}
	{...rest}
>
	{#each steps as step (step)}
		<span class={[block, step]}></span>
	{/each}
</span>
