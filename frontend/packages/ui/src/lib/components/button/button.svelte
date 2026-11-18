<script lang="ts" module>
	import { cn, type WithElementRef } from '../../utils/index.js';
	import type { HTMLAnchorAttributes, HTMLButtonAttributes } from 'svelte/elements';
	import { type VariantProps, tv } from 'tailwind-variants';

	// Sharp and quiet: the label is small uppercase with tracking (CSS `text-transform`, so the accessible name is
	// the text as written), the primary a solid high-contrast fill in the accent, the secondary a soft tint of it,
	// outline a 3:1 edge (WCAG 2.2 § 1.4.11), ghost nothing until hovered. A press sinks by a pixel.
	export const buttonVariants = tv({
		base: "focus-visible:ring-ring focus-visible:ring-offset-background aria-invalid:ring-destructive/40 aria-invalid:border-destructive group/button inline-flex shrink-0 items-center justify-center rounded-none border border-transparent bg-clip-padding text-[0.75rem] leading-none font-normal tracking-[0.07em] whitespace-nowrap uppercase transition-[background-color,border-color,color,box-shadow,translate] duration-(--duration-fast) outline-none select-none focus-visible:ring-2 focus-visible:ring-offset-1 active:not-aria-[haspopup]:translate-y-px aria-invalid:ring-2 disabled:pointer-events-none disabled:opacity-45 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
		variants: {
			variant: {
				default:
					'bg-primary text-primary-foreground hover:bg-primary-hover aria-expanded:bg-primary-hover',
				outline:
					'border-field-line bg-transparent text-foreground hover:bg-accent aria-expanded:bg-accent',
				secondary:
					'bg-secondary text-secondary-foreground hover:bg-secondary-hover aria-expanded:bg-secondary-hover aria-pressed:bg-secondary',
				ghost:
					'text-foreground hover:bg-accent aria-expanded:bg-accent aria-pressed:bg-secondary aria-pressed:text-secondary-foreground',
				destructive:
					'bg-destructive/12 text-destructive-ink hover:bg-destructive hover:text-destructive-foreground focus-visible:ring-destructive',
				link: 'text-primary tracking-normal normal-case underline-offset-4 hover:underline',
			},
			size: {
				default:
					'h-8 gap-2 px-3 has-data-[icon=inline-end]:pr-2.5 has-data-[icon=inline-start]:pl-2.5',
				xs: "h-6 gap-1 px-2 text-[0.6875rem] has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 [&_svg:not([class*='size-'])]:size-3",
				sm: "h-7 gap-1.5 px-2.5 text-[0.6875rem] has-data-[icon=inline-end]:pr-2 has-data-[icon=inline-start]:pl-2 [&_svg:not([class*='size-'])]:size-3.5",
				lg: 'h-10 gap-2 px-4 has-data-[icon=inline-end]:pr-3 has-data-[icon=inline-start]:pl-3',
				icon: 'size-8',
				'icon-xs': "size-6 [&_svg:not([class*='size-'])]:size-3",
				'icon-sm': 'size-7',
				'icon-lg': 'size-10',
			},
		},
		defaultVariants: {
			variant: 'default',
			size: 'default',
		},
	});

	export type ButtonVariant = VariantProps<typeof buttonVariants>['variant'];
	export type ButtonSize = VariantProps<typeof buttonVariants>['size'];

	export type ButtonProps = WithElementRef<HTMLButtonAttributes> &
		WithElementRef<HTMLAnchorAttributes> & {
			variant?: ButtonVariant;
			size?: ButtonSize;
		};
</script>

<script lang="ts">
	let {
		class: className,
		variant = 'default',
		size = 'default',
		ref = $bindable(null),
		href = undefined,
		type = 'button',
		disabled,
		children,
		...restProps
	}: ButtonProps = $props();
</script>

{#if href}
	<a
		bind:this={ref}
		data-slot="button"
		class={cn(buttonVariants({ variant, size }), className)}
		href={disabled ? undefined : href}
		aria-disabled={disabled}
		role={disabled ? 'link' : undefined}
		tabindex={disabled ? -1 : undefined}
		{...restProps}
	>
		{@render children?.()}
	</a>
{:else}
	<button
		bind:this={ref}
		data-slot="button"
		class={cn(buttonVariants({ variant, size }), className)}
		{type}
		{disabled}
		{...restProps}
	>
		{@render children?.()}
	</button>
{/if}
