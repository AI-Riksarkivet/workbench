<script lang="ts">
	// Adapted from Baby UI's `rolling-digits` (https://github.com/kanakkholwal/baby-ui, Apache-2.0; see THIRD_PARTY.md
	// in this package): its `odometer` variant alone, each digit a 0–9 strip that slides to its row with a CSS
	// transition (none under reduced motion), re-skinned to the tokens.
	import { cn } from '../../utils/cn.js';

	let {
		value,
		pad,
		format,
		live = true,
		class: className,
	}: {
		/** Rounded to an integer before it is shown. */
		value: number;
		/** Minimum digit count, zero-padded. */
		pad?: number;
		/** Formats the rounded value; its digits roll, anything else is shown as it is. */
		format?: (value: number) => string;
		/** Announce a change politely. Off where an accessible name already says the number (a counted button). */
		live?: boolean;
		class?: string;
	} = $props();

	const text = $derived.by(() => {
		const rounded = Number.isFinite(value) ? Math.round(value) : 0;
		if (format) return format(rounded);
		const digits = Math.abs(rounded)
			.toString()
			.padStart(pad && pad > 0 ? Math.floor(pad) : 0, '0');
		return `${rounded < 0 ? '-' : ''}${digits}`;
	});
	const chars = $derived([...text]);
	const ROWS = Array.from({ length: 10 }, (_, i) => i);
	const isDigit = (char: string): boolean => char >= '0' && char <= '9';

	// The strips start at 0 and turn to the value once mounted, so the first value turns too.
	let mounted = $state(false);
	$effect(() => {
		const frame = requestAnimationFrame(() => (mounted = true));
		return () => cancelAnimationFrame(frame);
	});
</script>

<span data-slot="rolling-digits" class={cn('inline-flex items-center tabular-nums', className)}>
	{#if live}
		<span aria-live="polite" class="sr-only">{text}</span>
	{:else}
		<span class="sr-only">{text}</span>
	{/if}
	<span aria-hidden="true" class="inline-flex items-center">
		<!-- Keyed from the right, so a digit keeps its column when the value gains or loses one. -->
		{#each chars as char, i (`${chars.length - i}-${isDigit(char) ? 'digit' : char}`)}
			{#if isDigit(char)}
				<span class="relative inline-block h-[1lh] overflow-hidden align-bottom">
					<span
						class="flex translate-y-[calc(var(--rd-index,0)*-1lh)] flex-col transition-[translate] duration-(--duration-slow) ease-(--ease-ui-out) motion-reduce:transition-none"
						style:--rd-index={mounted ? char : 0}
					>
						{#each ROWS as row (row)}
							<span class="h-[1lh] leading-[1lh]">{row}</span>
						{/each}
					</span>
				</span>
			{:else}
				<span>{char}</span>
			{/if}
		{/each}
	</span>
</span>
