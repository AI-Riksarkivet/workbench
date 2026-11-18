// Adapted from Baby UI's `tabs` (https://github.com/kanakkholwal/baby-ui, Apache-2.0; see THIRD_PARTY.md in this
// package): the variants cut to the two this design uses and re-skinned to its tokens.
import { tv, type VariantProps } from 'tailwind-variants';

// The list is the control height; triggers fill it. Labels are small uppercase with tracking (CSS, so the accessible
// name is the text as written); the selected tab is marked by the sliding indicator, measured from its trigger.
export const tabsFrame = tv({
	slots: {
		list: 'relative inline-flex w-max items-stretch',
		trigger:
			'text-muted-foreground hover:text-foreground focus-visible:ring-ring relative z-10 inline-flex shrink-0 items-center justify-center text-[0.75rem] leading-none tracking-[0.07em] whitespace-nowrap uppercase transition-colors duration-(--duration-fast) outline-none focus-visible:ring-2 disabled:pointer-events-none disabled:opacity-45 aria-selected:text-foreground [&_svg]:pointer-events-none [&_svg]:shrink-0',
		indicator:
			'pointer-events-none absolute left-0 transition-[transform,width,height] duration-(--duration-slow) ease-(--ease-drawer) motion-reduce:transition-none',
	},
	variants: {
		variant: {
			/** A rule under the strip, the selected tab underlined in the accent. */
			line: {
				list: 'border-border gap-5 border-b',
				trigger: 'px-0.5',
				indicator: 'bg-primary -bottom-px h-0.5',
			},
			/** A recessed strip, the selected tab a raised block of the card surface. */
			segment: {
				list: 'bg-muted gap-0.5 p-0.5',
				trigger: 'px-3',
				indicator: 'bg-card top-0.5 bottom-0.5',
			},
		},
		size: {
			sm: { list: 'h-7', trigger: 'gap-1.5 [&_svg]:size-3.5' },
			default: { list: 'h-9', trigger: 'gap-2 [&_svg]:size-4' },
		},
		orientation: {
			horizontal: {},
			vertical: { list: 'h-auto w-full flex-col', trigger: 'justify-start' },
		},
	},
	compoundVariants: [
		// A rule has no vertical form, so selection reads from the trigger text alone.
		{
			variant: 'line',
			orientation: 'vertical',
			class: { list: 'border-b-0', indicator: 'hidden' },
		},
		{ orientation: 'vertical', size: 'sm', class: { trigger: 'h-7' } },
		{ orientation: 'vertical', size: 'default', class: { trigger: 'h-9' } },
	],
	defaultVariants: { variant: 'line', size: 'default', orientation: 'horizontal' },
});

export type TabsVariant = NonNullable<VariantProps<typeof tabsFrame>['variant']>;
export type TabsSize = NonNullable<VariantProps<typeof tabsFrame>['size']>;
export type TabsOrientation = NonNullable<VariantProps<typeof tabsFrame>['orientation']>;
