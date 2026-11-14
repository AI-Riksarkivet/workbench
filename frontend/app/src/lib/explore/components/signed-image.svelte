<script lang="ts">
	/**
	 * An image whose `src` is a signed media link: a placeholder while the link is being signed, and
	 * when the image failed to load. The failure belongs to the `src` that failed, so a renewed link
	 * (a new `src`) is loaded again rather than staying hidden.
	 */
	import { cn } from '#lib/explore/utils.ts';

	type Props = {
		src: string | undefined;
		alt?: string;
		class?: string;
		loading?: 'lazy' | 'eager';
	};
	let { src, alt = '', class: className = '', loading = 'lazy' }: Props = $props();

	/** The `src` that failed to load, if the current one did. */
	let failed = $state<string | null>(null);
</script>

{#if src && failed !== src}
	<img {src} {alt} {loading} class={className} onerror={() => (failed = src ?? null)} />
{:else}
	<span
		class={cn('bg-muted block', className)}
		role={alt ? 'img' : undefined}
		aria-label={alt || undefined}
	></span>
{/if}
