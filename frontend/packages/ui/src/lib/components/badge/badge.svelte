<script lang="ts" module>
	import { type VariantProps, tv } from 'tailwind-variants';

	// A tag, not a pill: a sharp tinted block with small uppercase text and a square of its tone at the start. The tone
	// is carried by the square and the tint; the text stays the foreground, so every tone reads at AA on every theme.
	export const badgeVariants = tv({
		base: "focus-visible:ring-ring aria-invalid:ring-destructive/40 inline-flex h-5 w-fit shrink-0 items-center justify-center gap-1.5 overflow-hidden rounded-none border-0 px-1.5 text-[0.6875rem] leading-none tracking-[0.06em] whitespace-nowrap text-foreground uppercase transition-[color,background-color,box-shadow] before:size-1.5 before:shrink-0 before:bg-(--badge-tone) before:content-[''] focus-visible:ring-2 [&>svg]:pointer-events-none [&>svg]:size-3",
		variants: {
			variant: {
				default: 'bg-primary/14 [--badge-tone:var(--primary)] [a&]:hover:bg-primary/22',
				secondary: 'bg-muted [--badge-tone:var(--muted-foreground)] [a&]:hover:bg-accent',
				destructive:
					'bg-destructive/16 [--badge-tone:var(--destructive)] [a&]:hover:bg-destructive/24',
				outline:
					'bg-transparent shadow-[inset_0_0_0_1px_var(--border-strong)] [--badge-tone:var(--border-strong)] [a&]:hover:bg-accent',
				success: 'bg-success/16 [--badge-tone:var(--success)] [a&]:hover:bg-success/24',
				warning: 'bg-warning/22 [--badge-tone:var(--warning)] [a&]:hover:bg-warning/30',
			},
		},
		defaultVariants: {
			variant: 'default',
		},
	});

	export type BadgeVariant = VariantProps<typeof badgeVariants>['variant'];
</script>

<script lang="ts">
	import type { HTMLAnchorAttributes } from 'svelte/elements';
	import { cn } from '../../utils/cn.js';
	import type { WithElementRef } from '../../utils/types.js';

	let {
		ref = $bindable(null),
		href,
		class: className,
		variant = 'default',
		children,
		...restProps
	}: WithElementRef<HTMLAnchorAttributes> & {
		variant?: BadgeVariant;
	} = $props();
</script>

<svelte:element
	this={href ? 'a' : 'span'}
	bind:this={ref}
	data-slot="badge"
	{href}
	class={cn(badgeVariants({ variant }), className)}
	{...restProps}
>
	{@render children?.()}
</svelte:element>
