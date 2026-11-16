<script lang="ts">
	import { Slider } from 'bits-ui';
	import type { Snippet } from 'svelte';
	import { cn } from '../../utils/index.js';

	let {
		value = $bindable(50),
		min = 0,
		max = 100,
		step = 1,
		class: className,
		'aria-label': ariaLabel,
		'aria-valuetext': ariaValueText,
		track,
		trackClass,
		...rest
	}: {
		value?: number;
		min?: number;
		max?: number;
		step?: number;
		disabled?: boolean;
		class?: string;
		/** The slider's accessible name. It is the thumb's, since the thumb carries `role="slider"`. */
		'aria-label'?: string;
		/** The value as a person reads it ("0:04 of 0:06"), where the bare number says less. */
		'aria-valuetext'?: string;
		/** Drawn inside the track, over the filled range: marks on the axis, such as a recording's segments. */
		track?: Snippet;
		/** The track's own classes, merged over its defaults (a scrubber's taller track). */
		trackClass?: string;
		/** Fires on user interaction only (not programmatic value changes) — lets a
		 *  controlled scrubber seek without a bind:value ↔ update loop. */
		onValueChange?: (value: number) => void;
	} = $props();
</script>

<Slider.Root
	type="single"
	bind:value
	{min}
	{max}
	{step}
	data-slot="slider"
	class={cn('relative flex w-full touch-none items-center select-none', className)}
	{...rest}
>
	{#snippet children({ thumbs })}
		<span
			class={cn('bg-secondary relative h-1.5 w-full grow overflow-hidden rounded-full', trackClass)}
			data-slot="slider-track"
		>
			<Slider.Range class="bg-primary absolute h-full" />
			{@render track?.()}
		</span>
		{#each thumbs as index (index)}
			<Slider.Thumb
				{index}
				aria-label={ariaLabel}
				aria-valuetext={ariaValueText}
				class="border-primary bg-background focus-visible:ring-ring block size-4 rounded-full border-2 shadow transition-colors focus-visible:ring-2 focus-visible:outline-none"
			/>
		{/each}
	{/snippet}
</Slider.Root>
