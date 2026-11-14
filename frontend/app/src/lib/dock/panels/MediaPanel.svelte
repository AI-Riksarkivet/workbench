<script lang="ts">
	/**
	 * The picked item's media, through a signed link: the link is the permission to read that one cell
	 * for minutes, so `<img>` and `<video>`, which send no token, can load it same-origin.
	 */
	import { signedLinks } from '#lib/api/media.api.ts';
	import { bench } from '../bench.svelte.ts';

	const dataset = $derived(bench.dataset);
	const selected = $derived(bench.selected);
	const link = $derived(
		dataset && selected ? signedLinks({ dataset, role: 'media', docIds: [selected] }) : null,
	);
	const media = $derived(selected ? link?.current?.get(selected) : undefined);
	const kind = $derived(
		bench.selectedMime?.startsWith('video/')
			? 'video'
			: bench.selectedMime?.startsWith('audio/')
				? 'audio'
				: 'image',
	);
</script>

<section class="flex h-full min-h-0 flex-col p-2" aria-labelledby="media-heading">
	<h2 id="media-heading" class="text-muted-foreground mb-2 truncate text-xs">
		{#if selected}<span class="font-mono">{selected}</span>{:else}Media{/if}
	</h2>
	{#if !selected}
		<p class="text-muted-foreground text-sm">Pick an item in Results to see its media here.</p>
	{:else if link?.error}
		<p class="text-destructive text-sm" role="alert">The media link could not be signed.</p>
	{:else if !media}
		<p class="text-muted-foreground text-sm" role="status">Signing the media link…</p>
	{:else if 'detail' in media}
		<p class="text-muted-foreground text-sm">No media: {media.detail}</p>
	{:else if kind === 'video'}
		<!-- svelte-ignore a11y_media_has_caption -->
		<video src={media.url} controls class="min-h-0 w-full flex-1 rounded-md bg-black"></video>
	{:else if kind === 'audio'}
		<audio src={media.url} controls class="w-full"></audio>
	{:else}
		<img
			src={media.url}
			alt={`Media of ${selected}`}
			class="min-h-0 w-full flex-1 object-contain"
		/>
	{/if}
</section>
