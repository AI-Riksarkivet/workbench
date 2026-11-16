<script lang="ts">
	/**
	 * A row's preview in a tile or a table: its image (`previewUrl`) when the dataset has one for it, else
	 * an icon of the dataset's media kind, so a dataset with no image to show never renders an empty box.
	 */
	import { AudioLines, FileText, Film, Image } from '@lucide/svelte';
	import { viewForHit, type Row } from '@workbench/explorer-api';
	import SignedImage from '#lib/explore/components/signed-image.svelte';
	import { previewUrl } from '#lib/explore/media.ts';
	import { cn } from '#lib/explore/utils.ts';

	let { row, class: className = '' }: { row: Row; class?: string } = $props();

	const url = $derived(previewUrl(row));
	const kind = $derived(viewForHit(row).mediaKind);
	const Icon = $derived(
		kind === 'audio' ? AudioLines : kind === 'video' ? Film : kind === 'image' ? Image : FileText,
	);
	const label = $derived(
		kind === 'audio'
			? 'A recording'
			: kind === 'video'
				? 'A video'
				: kind === 'image'
					? 'An image'
					: 'A text',
	);
</script>

{#if url === null}
	<span
		class={cn('bg-muted text-muted-foreground flex items-center justify-center', className)}
		role="img"
		aria-label={label}
	>
		<Icon class="size-5" aria-hidden="true" />
	</span>
{:else}
	<SignedImage src={url} class={className} />
{/if}
