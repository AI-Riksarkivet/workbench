<script lang="ts" module>
	/** The block ramp's five steps, darkest last: a filled meter walks the ramp from its first block to its last. */
	const RAMP = ['bg-block-1', 'bg-block-2', 'bg-block-3', 'bg-block-4', 'bg-block-5'] as const;

	/** The ramp step a filled block takes: its place along the whole meter, so a full meter shows the whole ramp. */
	export function rampStep(index: number, segments: number): (typeof RAMP)[number] {
		const at =
			segments <= 1 ? RAMP.length - 1 : Math.round((index / (segments - 1)) * (RAMP.length - 1));
		return RAMP[Math.min(RAMP.length - 1, Math.max(0, at))] ?? 'bg-block-4';
	}
</script>

<script lang="ts">
	import { Progress as BitsProgress, type WithoutChildrenOrChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	// The meter is the block motif: a row of square blocks, filled from the start in the accent's ramp. Bits UI's root
	// keeps the progressbar role and its values; the blocks are decoration on it.
	let {
		ref = $bindable(null),
		class: className,
		value,
		max = 100,
		segments = 16,
		...rest
	}: WithoutChildrenOrChild<BitsProgress.RootProps> & {
		/** How many blocks the meter is drawn with. */
		segments?: number;
	} = $props();

	const filled = $derived(
		max > 0 && value !== null && value !== undefined
			? Math.min(segments, Math.max(0, Math.round((value / max) * segments)))
			: 0,
	);
</script>

<BitsProgress.Root
	bind:ref
	{value}
	{max}
	data-slot="progress"
	class={cn('grid h-1.5 w-full gap-0.5', className)}
	style="grid-template-columns: repeat({segments}, minmax(0, 1fr))"
	{...rest}
>
	{#each { length: segments }, index}
		<span
			aria-hidden="true"
			data-slot="progress-block"
			data-filled={index < filled}
			class={[
				'h-full transition-colors duration-(--duration-base)',
				index < filled ? rampStep(index, segments) : 'bg-field-line/22',
			]}
		></span>
	{/each}
</BitsProgress.Root>
