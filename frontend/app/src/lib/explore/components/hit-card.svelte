<script lang="ts">
	import SignedImage from '#lib/explore/components/signed-image.svelte';
	import RowPreview from '#lib/explore/components/row-preview.svelte';
	import { type Hit, viewForHit } from '@workbench/explorer-api';
	import { chunkFrameUrl } from '#lib/explore/media.ts';
	import { activeView } from '@workbench/explorer-api/descriptor';
	import { audioPreview } from '#lib/explore/audio-preview.svelte.ts';
	import { fmtTime, queryTerms, makeHighlighter, cn, hitKey } from '#lib/explore/utils.ts';
	import { AudioLines, FileText, Film, Image as ImageIcon, Play, Pause } from '@lucide/svelte';

	type Props = {
		hit: Hit;
		query?: string;
		active?: boolean;
		/** "row" = side-by-side (list); "tile" = thumbnail on top (grid). */
		layout?: 'row' | 'tile';
		/**
		 * Prebuilt highlighter (HTML-escapes + wraps query matches). A list renders
		 * one card per hit, so the parent (hit-list / the grid) compiles the match
		 * RegExp ONCE and passes it down — at ~1000 cards this avoids ~1000 regex
		 * compilations. When omitted (standalone use), we fall back to deriving it
		 * from `query` here so the card stays self-contained.
		 */
		highlight?: (text: string) => string;
		onclick?: () => void;
	};
	let {
		hit,
		query = '',
		active = false,
		layout = 'row',
		highlight: highlightProp,
		onclick,
	}: Props = $props();

	// The active dataset view — every corpus field (title / body / caption /
	// metadata / time / media URLs) is read through it, so this card renders any
	// dataset's schema instead of hardcoded columns.
	const view = activeView();
	const docId = $derived(view.docId(hit));
	const title = $derived(view.title(hit));
	const body = $derived(view.body(hit));
	const caption = $derived(view.caption(hit));
	const time = $derived(view.time(hit));
	// The chunk's frame beside the thumbnail, signed with the page's other media; none until it is, when it has none,
	// or when the dataset declares no thumbnail, where the preview itself is the frame (`previewUrl`).
	const frame = $derived(viewForHit(hit).hasThumbnail ? chunkFrameUrl(hit) : undefined);
	const highlight = $derived(highlightProp ?? makeHighlighter(queryTerms(query)));
	// A caption is marked with the icon of the dataset's own media kind, so a page is not shown as a film.
	const captionKind = $derived(viewForHit(hit).mediaKind);
	const CaptionIcon = $derived(
		captionKind === 'audio'
			? AudioLines
			: captionKind === 'video'
				? Film
				: captionKind === 'image'
					? ImageIcon
					: FileText,
	);

	// Compact meta line: the time span (when the dataset has one) followed by the
	// declared metadata values (empties already dropped), skipping any row that
	// just repeats the title. ` · `-joined, truncated to a single line.
	const metaLine = $derived.by(() => {
		const segs: string[] = [];
		if (time) segs.push(`${fmtTime(time.start)} → ${fmtTime(time.end)}`);
		for (const m of view.metadata(hit)) if (m.value !== title) segs.push(m.value);
		return segs.join('  ·  ');
	});
</script>

{#snippet playButton(extra: string)}
	<!-- Per-card audio preview — the same shared one-element audioPreview store
       (and row key) as the table's play column, so state syncs across views. -->
	{@const rowKey = hitKey(hit)}
	{@const playing = audioPreview.isPlaying(rowKey)}
	{@const failed = audioPreview.isFailed(docId)}
	{@const clipStart = time?.start ?? 0}
	{@const clipEnd = time?.end ?? 0}
	<button
		type="button"
		disabled={failed}
		title={failed
			? 'Audio unavailable — media failed to load'
			: playing
				? 'Pause'
				: `Play ${fmtTime(clipStart)}–${fmtTime(clipEnd)}`}
		aria-label={playing ? 'Pause clip' : 'Play clip'}
		aria-pressed={playing}
		onclick={(e) => {
			e.stopPropagation();
			audioPreview.toggle({ key: rowKey, docId, start: clipStart, end: clipEnd });
		}}
		class={cn(
			'focus-visible:ring-ring inline-flex size-6 items-center justify-center rounded-md transition-opacity focus-visible:opacity-100 focus-visible:ring-2 focus-visible:outline-none disabled:cursor-not-allowed',
			extra,
			playing ? 'opacity-100' : 'opacity-0 group-hover:opacity-100',
			failed && 'group-hover:opacity-40',
		)}
	>
		{#if playing}
			<Pause class="size-3.5" />
		{:else}
			<Play class="size-3.5" />
		{/if}
	</button>
{/snippet}

{#if layout === 'tile'}
	<!-- Relative wrapper: the card root is a <button>, so the play action must
       be a SIBLING overlay (nested buttons are invalid HTML). -->
	<div class="group relative h-full" data-hit-key={hitKey(hit)}>
		<button
			type="button"
			{onclick}
			aria-pressed={active}
			class={cn(
				'bg-card flex h-full w-full flex-col overflow-hidden rounded-lg border text-left transition-all',
				'focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none',
				active
					? 'border-primary ring-primary -translate-y-0.5 shadow-md ring-2'
					: 'border-border hover:border-primary hover:-translate-y-0.5 hover:shadow-md',
			)}
		>
			<div class="bg-muted relative aspect-video w-full overflow-hidden">
				<RowPreview
					row={hit}
					class="h-full w-full object-cover transition-transform group-hover:scale-105"
				/>
				{#if frame}
					<SignedImage
						src={frame}
						class="border-background absolute right-1.5 bottom-1.5 h-10 w-16 rounded-md border bg-black object-cover shadow"
					/>
				{/if}
				{#if time}
					<span
						class="absolute bottom-1.5 left-1.5 rounded-md bg-black/70 px-1.5 py-0.5 font-mono text-[0.7rem] text-white"
					>
						{fmtTime(time.start)}
					</span>
				{/if}
			</div>
			<div class="flex flex-1 flex-col gap-1 p-2.5">
				<div class="line-clamp-1 text-xs leading-snug font-semibold">{title}</div>
				{#if metaLine}
					<div class="text-muted-foreground truncate font-mono text-[0.7rem]">{metaLine}</div>
				{/if}
				<div class="line-clamp-3 text-xs leading-snug [overflow-wrap:anywhere]">
					<!-- highlight() HTML-escapes then wraps matches — safe to inject -->
					<!-- eslint-disable-next-line svelte/no-at-html-tags -->
					{@html highlight(body)}
				</div>
				{#if caption}
					<div class="text-muted-foreground line-clamp-2 text-[0.7rem] italic" title={caption}>
						<CaptionIcon class="mr-0.5 inline size-3 align-[-2px]" aria-hidden="true" />{caption}
					</div>
				{/if}
			</div>
		</button>
		<div class="absolute top-1.5 right-1.5 z-[2] flex gap-1">
			{#if view.isPlayable}
				{@render playButton('bg-black/60 text-white enabled:hover:bg-primary')}
			{/if}
		</div>
	</div>
{:else}
	<div class="group relative" data-hit-key={hitKey(hit)}>
		<button
			type="button"
			{onclick}
			aria-pressed={active}
			class={cn(
				'border-border flex w-full items-start gap-3 border-b px-3 py-2.5 text-left transition-colors',
				'hover:bg-muted/60',
				// ring-inset keeps the highlight inside the row's box so it survives
				// the parent's overflow-clip; bg + thick left bar make selection
				// unmistakable in both light and dark themes.
				active &&
					'bg-primary/15 ring-primary relative z-[1] shadow-[inset_4px_0_0_0_var(--color-primary)] ring-2 ring-inset',
			)}
		>
			<div class="relative flex-none">
				<RowPreview row={hit} class="h-[54px] w-[96px] rounded-md object-cover" />
				{#if frame}
					<SignedImage
						src={frame}
						class="border-background absolute -right-0.5 -bottom-0.5 h-5 w-9 rounded-sm border bg-black object-cover"
					/>
				{/if}
			</div>

			<div class="flex min-w-0 flex-1 flex-col gap-0.5">
				<div class="line-clamp-2 text-sm leading-snug font-semibold [overflow-wrap:anywhere]">
					{title}
				</div>
				{#if metaLine}
					<div class="text-muted-foreground truncate font-mono text-xs">{metaLine}</div>
				{/if}
				<div class="line-clamp-3 text-sm leading-snug [overflow-wrap:anywhere]">
					<!-- highlight() HTML-escapes then wraps matches — safe to inject -->
					<!-- eslint-disable-next-line svelte/no-at-html-tags -->
					{@html highlight(body)}
				</div>
				{#if caption}
					<div class="text-muted-foreground line-clamp-1 text-xs italic" title={caption}>
						<CaptionIcon class="mr-0.5 inline size-3 align-[-2px]" aria-hidden="true" />{caption}
					</div>
				{/if}
			</div>
		</button>
		<div class="absolute top-2 right-2 z-[2] flex gap-1">
			{#if view.isPlayable}
				{@render playButton(
					'border border-border bg-card text-muted-foreground shadow-sm enabled:hover:text-primary',
				)}
			{/if}
		</div>
	</div>
{/if}
