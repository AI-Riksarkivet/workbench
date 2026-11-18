// Adapted from Baby UI's `hold-to-confirm` (Apache-2.0; see THIRD_PARTY.md in this package), re-skinned to the tokens:
// sharp, small uppercase like every button, the fill the destructive colour.
import { tv, type VariantProps } from 'tailwind-variants';

// The fill is a second, inverted copy of the face revealed by clip-path, so the text flips colour exactly at the
// fill's edge.
export const holdToConfirm = tv({
	slots: {
		root: 'group/hold focus-visible:ring-ring focus-visible:ring-offset-background relative inline-flex touch-manipulation items-center justify-center rounded-none border text-[0.75rem] leading-none tracking-[0.07em] uppercase transition-[background-color,border-color,opacity,scale] duration-(--duration-fast) ease-(--ease-ui-out) outline-none select-none [-webkit-tap-highlight-color:transparent] [-webkit-touch-callout:none] focus-visible:ring-2 focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-45 aria-disabled:cursor-default data-holding:scale-[0.98] motion-reduce:transition-none',
		fill: 'pointer-events-none absolute -inset-px flex items-center justify-center',
		face: 'inline-flex items-center gap-2 whitespace-nowrap',
		icon: 'grid size-[1.125em] flex-none place-items-center [&_svg]:size-[1.125em]',
		// pathLength=1 lets one dash draw the tick from its short stroke once the hold completes.
		check:
			'transition-[stroke-dashoffset] delay-75 duration-300 ease-(--ease-ui-out) [stroke-dasharray:1] [stroke-dashoffset:1] group-data-[state=done]/hold:[stroke-dashoffset:0] motion-reduce:transition-none',
	},
	variants: {
		variant: {
			default: {
				root: 'border-field-line text-foreground hover:bg-accent data-[state=armed]:bg-accent bg-transparent',
				fill: 'bg-primary text-primary-foreground',
			},
			destructive: {
				root: 'border-destructive/45 text-destructive-ink hover:bg-destructive/10 data-[state=armed]:bg-destructive/14 data-[state=armed]:border-destructive bg-transparent',
				fill: 'bg-destructive text-destructive-foreground',
			},
		},
		size: {
			xs: { root: 'h-6 px-2 text-[0.6875rem]' },
			sm: { root: 'h-7 px-2.5 text-[0.6875rem]' },
			default: { root: 'h-8 px-3' },
		},
	},
	defaultVariants: { variant: 'default', size: 'default' },
});

export type HoldToConfirmVariant = NonNullable<VariantProps<typeof holdToConfirm>['variant']>;
export type HoldToConfirmSize = NonNullable<VariantProps<typeof holdToConfirm>['size']>;
