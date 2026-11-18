<script lang="ts">
	// A field is a filled surface with one edge, the bottom, at 3:1 against the page (WCAG 2.2 § 1.4.11); focus turns
	// that edge into a two-pixel line of the accent.
	import type { HTMLInputAttributes, HTMLInputTypeAttribute } from 'svelte/elements';
	import { cn, type WithElementRef } from '../../utils/index.js';

	type InputType = Exclude<HTMLInputTypeAttribute, 'file'>;

	type Props = WithElementRef<
		Omit<HTMLInputAttributes, 'type'> &
			({ type: 'file'; files?: FileList } | { type?: InputType; files?: undefined })
	>;

	let {
		ref = $bindable(null),
		value = $bindable(),
		type,
		files = $bindable(),
		class: className,
		'data-slot': dataSlot = 'input',
		...restProps
	}: Props = $props();
</script>

{#if type === 'file'}
	<input
		bind:this={ref}
		data-slot={dataSlot}
		class={cn(
			'bg-input border-field-line placeholder:text-muted-foreground hover:bg-accent/60 focus-visible:border-primary focus-visible:shadow-[inset_0_-1px_0_0_var(--primary)] aria-invalid:border-destructive aria-invalid:shadow-[inset_0_-1px_0_0_var(--destructive)] w-full min-w-0 rounded-none border-0 border-b px-2.5 text-base transition-[background-color,border-color,box-shadow] duration-(--duration-fast) outline-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm file:text-foreground h-8 py-1 file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-xs file:uppercase',
			className,
		)}
		type="file"
		bind:files
		bind:value
		{...restProps}
	/>
{:else}
	<input
		bind:this={ref}
		data-slot={dataSlot}
		class={cn(
			'bg-input border-field-line placeholder:text-muted-foreground hover:bg-accent/60 focus-visible:border-primary focus-visible:shadow-[inset_0_-1px_0_0_var(--primary)] aria-invalid:border-destructive aria-invalid:shadow-[inset_0_-1px_0_0_var(--destructive)] w-full min-w-0 rounded-none border-0 border-b px-2.5 text-base transition-[background-color,border-color,box-shadow] duration-(--duration-fast) outline-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm file:text-foreground h-8 py-1 file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-xs file:uppercase',
			className,
		)}
		{type}
		bind:value
		{...restProps}
	/>
{/if}
