<script lang="ts">
	import type { ThemeId } from './core.js';

	// A theme's accent ramp in three blocks, drawn from THAT theme's seeds (`data-theme` on the swatch itself), in the
	// page's current mode: what picking it would look like.
	let { theme }: { theme: ThemeId } = $props();
</script>

<span class="swatch" data-theme={theme} aria-hidden="true"
	><span></span><span></span><span></span></span
>

<style>
	.swatch {
		--sw-l: var(--accent-l-light);
		--sw-c: var(--accent-c-light);
		display: inline-flex;
		gap: 1px;
		flex-shrink: 0;
	}
	:global(.dark) .swatch {
		--sw-l: var(--accent-l-dark);
		--sw-c: var(--accent-c-dark);
	}
	.swatch > span {
		width: 0.5rem;
		height: 0.75rem;
	}
	.swatch > span:nth-child(1) {
		background: oklch(
			clamp(0.2, calc(var(--sw-l) + var(--ramp-dir) * 0.26), 0.95) calc(var(--sw-c) * 0.66)
				calc(var(--accent-h) + 20)
		);
	}
	.swatch > span:nth-child(2) {
		background: oklch(var(--sw-l) var(--sw-c) var(--accent-h));
	}
	.swatch > span:nth-child(3) {
		background: oklch(
			clamp(0.2, calc(var(--sw-l) - var(--ramp-dir) * 0.12), 0.95) calc(var(--sw-c) * 0.92)
				calc(var(--accent-h) - 8)
		);
	}
</style>
