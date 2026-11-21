<script lang="ts">
	/**
	 * The viewer of the item the reader opened, one layout for every kind: a header that says what is open and where
	 * in it the reader is (a page of a document, a moment of a recording), the media filling the height left, the
	 * transport for anything that plays, the views synced to it that the dataset declares (transcript, speakers), and
	 * the item's metadata.
	 *
	 * The viewers themselves are the app's shared ones (`#lib/media`): the still on the WebGPU image stage, the
	 * transport and the transcript on the recording's time axis. This pane is what the explorer adds: which rows are
	 * a document's pages and segments, the signed links, the speakers, the failures and how to ask again.
	 */
	import { untrack } from 'svelte';
	import {
		type Hit,
		type DocChunk,
		type DiarTurn,
		activeView,
		viewForHit,
	} from '@workbench/explorer-api';
	import { frameLink, mediaLink, resolveFrame, resolveMedia } from '#lib/explore/media.ts';
	import { linkMayHaveExpired, resumeAfterSwap } from '#lib/explore/media-source.ts';
	import { documentChunks, speakerTurns } from '#lib/api/explore.api.ts';
	import { fmtTime, hitKey, mediaNoun, queryTerms } from '#lib/explore/utils.ts';
	import { partIndexOf, turnedTo } from '#lib/explore/doc-parts.ts';
	import {
		AudioLines,
		ChevronLeft,
		ChevronRight,
		Maximize2,
		Minimize2,
		MousePointerClick,
	} from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui';
	import { panesFor, shownTab } from '#lib/explore/pane-capabilities.ts';
	import DiarizationTimeline from '#lib/explore/components/diarization-timeline.svelte';
	import LoadFailure from '#lib/shell/load-failure.svelte';
	import ImageStage, {
		type ImageStageContext,
		type ImageStageFailure,
	} from '#lib/media/image/ImageStage.svelte';
	import MediaTransport from '#lib/media/time/MediaTransport.svelte';
	import TimeStrip from '#lib/media/time/TimeStrip.svelte';
	import { rgba } from '#lib/gpu/rects.ts';
	import type { StripRow } from '#lib/media/time/strip.ts';
	import { wholeView } from '#lib/media/time/view.ts';
	import { documentPeaks } from '#lib/api/timeline.api.ts';
	import TranscriptView, { type TranscriptLine } from '#lib/media/time/TranscriptView.svelte';
	import { spanAt, type Span } from '#lib/media/time/timeline.ts';
	import { on } from 'svelte/events';

	type Props = {
		hit: Hit | null;
		query?: string;
		/** What the empty player tells the reader to do, in the words of the page it is on. */
		idle?: string;
	};
	let { hit, query = '', idle = 'Click a hit to play.' }: Props = $props();

	/** The view THIS hit is rendered through: a fused result set carries rows of several datasets, and each names its
	 *  own (`_dataset`, stamped by search). A hit that names none is the active dataset's. */
	const view = $derived(hit ? viewForHit(hit) : activeView());
	const mediaKind = $derived(view.mediaKind);
	const noun = $derived(mediaNoun(mediaKind));
	const plays = $derived(mediaKind === 'audio' || mediaKind === 'video');
	// Which views this dataset may show, from what it DECLARES: a still has no time axis, no transcript, no speakers.
	const panes = $derived(panesFor(view));

	// ── the document's parts: its pages, or a recording's segments, in order ──
	const docKey = $derived(hit ? { dataset: view.id, docId: view.docId(hit) } : null);
	const chunksRead = $derived(docKey ? documentChunks(docKey) : null);
	const docChunks = $derived<DocChunk[]>(chunksRead?.current?.chunks ?? []);
	const turnsRead = $derived(docKey && panes.speakers ? speakerTurns(docKey) : null);
	const diarTurns = $derived<DiarTurn[]>(turnsRead?.current?.turns ?? []);
	/** Where the opened row is among its document's parts; -1 when the document does not list it. */
	const hitPart = $derived(hit ? partIndexOf(view, docChunks, hit) : -1);

	// ── pages: a still-image document is read a page at a time, in the order its rows are listed ──
	/** Which opened row the pager's memory is for: the pane mounts again when the dock moves or maximizes it, and
	 *  the reader stays on the page they turned to. */
	const opened = $derived(hit ? `${view.id}\u0000${hitKey(hit)}` : '');
	/** The page shown, an index into the document's parts; -1 shows the opened row itself (its document lists no
	 *  parts, or not this one). A newly opened row starts on its own page. */
	const page = $derived(
		mediaKind !== 'image' || hitPart < 0
			? -1
			: Math.min(docChunks.length - 1, turnedTo.get(opened) ?? hitPart),
	);
	const pageCount = $derived(page >= 0 ? docChunks.length : 0);
	/** The row the still shows: the page's own keys over the opened row's document and dataset. */
	const shownRow = $derived(
		hit && page >= 0 && docChunks[page] ? { ...hit, ...docChunks[page] } : hit,
	);

	function goToPage(i: number): void {
		if (pageCount === 0) return;
		turnedTo.set(opened, Math.min(pageCount - 1, Math.max(0, i)));
	}

	// ── links: each a signed one (media elements send no token), or the viewer's reason when it named none ──
	const media = $derived(hit && plays ? mediaLink(hit) : undefined);
	const frame = $derived(shownRow && mediaKind === 'image' ? frameLink(shownRow) : undefined);

	// The playing element keeps ONE source per document (`media-source.ts`): the loader renews its links on its own
	// schedule, and following each renewal would restart playback. The first link for a document is latched here; a
	// later one replaces it only when the element's source stopped serving.
	const mediaKey = $derived(hit ? `${view.id}\u0000${view.docId(hit)}` : null);
	let source = $state<{ key: string; url: string } | null>(null);
	$effect(() => {
		const key = mediaKey;
		const url = media?.url;
		if (key !== null && url && untrack(() => source?.key) !== key) source = { key, url };
	});
	const mediaSrc = $derived(source !== null && source.key === mediaKey ? source.url : undefined);
	/** The link a renewal swapped in: if that one fails too, the failure is reported, not renewed. */
	let renewedTo: string | null = null;

	/** The element's source stopped serving: sign a fresh link and swap it in where playback was. */
	async function renewSource(el: HTMLMediaElement): Promise<boolean> {
		const h = hit;
		const key = mediaKey;
		if (!h || key === null || renewedTo === mediaSrc) return false;
		const link = await resolveMedia(h, true);
		if (link.url === null || key !== mediaKey) return false;
		renewedTo = link.url;
		resumeAfterSwap(el);
		source = { key, url: link.url };
		return true;
	}

	const shownLink = $derived(mediaKind === 'image' ? frame : media);
	const linkFailure = $derived(shownLink?.url === null ? shownLink.detail : null);

	// What the image stage could not do. No WebGPU is the browser's and outlives the picture; a picture that did not
	// load is that picture's, so a new one clears it.
	let gpuFailure = $state<string | null>(null);
	let pictureFailure = $derived.by((): string | null => {
		void frame?.url;
		return null;
	});
	function onStageError(failure: ImageStageFailure): void {
		if (failure.kind === 'gpu') gpuFailure = failure.message;
		else pictureFailure = failure.message;
	}

	/** Read again everything the pane shows for the hit: its media link, its parts and its speaker turns. */
	function retryAll(): void {
		const h = shownRow;
		if (!h) return;
		mediaError = null;
		pictureFailure = null;
		void (mediaKind === 'image' ? resolveFrame(h, true) : resolveMedia(h, true));
		void chunksRead?.refresh();
		if (panes.speakers) void turnsRead?.refresh();
	}

	const reason = (err: unknown): string => (err instanceof Error ? err.message : String(err));
	// What the synced views could not read, in the reader's words: the parts feed the scrubber's segments and the
	// transcript, the turns the Speakers tab. A read the dataset does not use is no failure of this item.
	const viewsFailure = $derived.by((): { what: string; detail: string } | null => {
		const chunksFailed = (panes.timeline || panes.transcript) && chunksRead?.error !== undefined;
		const turnsFailed = panes.speakers && turnsRead?.error !== undefined;
		if (chunksFailed && turnsFailed)
			return {
				what: "This item's transcript, segments and speakers could not be loaded.",
				detail: `${reason(chunksRead?.error)}; ${reason(turnsRead?.error)}`,
			};
		if (chunksFailed)
			return {
				what: "This item's transcript and segments could not be loaded.",
				detail: reason(chunksRead?.error),
			};
		if (turnsFailed)
			return {
				what: "This item's speakers could not be loaded.",
				detail: reason(turnsRead?.error),
			};
		return null;
	});

	// ── time: the element's own state, bound, and handed to the transport and the synced views ──
	let mediaEl = $state<HTMLVideoElement | HTMLAudioElement | null>(null);
	let mediaError = $state<string | null>(null);
	let currentTime = $state(0);
	let duration = $state(0);
	let paused = $state(true);
	let volume = $state(1);
	let muted = $state(false);
	let playbackRate = $state(1);

	/** The document's timed parts, as the scrubber's segments, each with its index among the parts. */
	const timed = $derived.by((): (Span & { part: number })[] => {
		const out: (Span & { part: number })[] = [];
		docChunks.forEach((c, part) => {
			const t = view.time(c);
			if (t) out.push({ start: t.start, end: t.end, part });
		});
		return out;
	});

	/** The part the playhead is in: the timed part that holds it (or, in a gap, the last one before it); before the
	 *  playhead moves, the part the reader opened, so the right segment shows the moment a row opens. */
	const currentPart = $derived.by((): number => {
		if (docChunks.length === 0) return -1;
		const i = spanAt(timed, currentTime);
		if (i >= 0 && (currentTime > 0 || hitPart < 0)) return timed[i]?.part ?? -1;
		return hitPart >= 0 ? hitPart : 0;
	});
	const currentSegment = $derived(timed.findIndex((s) => s.part === currentPart));

	// ── the time strip: the recording's waveform and its segments on one axis, the labeling canvas's own, read-only ──
	const peaksRead = $derived(
		hit && plays ? documentPeaks({ dataset: view.id, docId: view.docId(hit) }) : null,
	);
	const wave = $derived(peaksRead?.current?.ok ? peaksRead.current.data : null);
	const waveNote = $derived(
		!peaksRead || peaksRead.current === undefined
			? 'Reading the waveform…'
			: !peaksRead.current.ok
				? `No waveform: ${peaksRead.current.detail}`
				: !peaksRead.current.data.audio
					? `This ${noun} has no sound.`
					: null,
	);
	let stripView = $state(wholeView(0));
	const stripRows = $derived.by<StripRow[]>(() => {
		if (timed.length === 0) return [];
		const ink = rgba('#94a3b8', 0.8);
		return [
			{
				id: 'parts',
				label: mediaKind === 'video' ? 'Shots' : 'Segments',
				height: 16,
				spans: timed.map((s, i) => ({
					key: `part:${s.part}`,
					start: s.start,
					end: s.end,
					colour: ink,
					selected: i === currentSegment,
				})),
			},
		];
	});

	// ── a video's frames, drawn by the shared image stage from the hidden element ──
	let videoStage: ImageStageContext | null = null;
	function onVideoStage(stage: ImageStageContext): () => void {
		videoStage = stage;
		const el = mediaEl;
		let alive = true;
		if (el instanceof HTMLVideoElement && 'requestVideoFrameCallback' in el) {
			const onframe = () => {
				if (!alive) return;
				stage.image.showVideoFrame();
				el.requestVideoFrameCallback(onframe);
			};
			el.requestVideoFrameCallback(onframe);
		}
		return () => {
			alive = false;
			videoStage = null;
		};
	}

	// The playing segment's span, for the speakers' segment zoom; the opened row's before the parts are read.
	const segmentSpan = $derived.by((): Span | null => {
		const c = docChunks[currentPart] ?? hit;
		return c ? viewForHit(c).time(c) : null;
	});

	/** The transcript: each part's aligned lines, or its text over its span when it has no alignment. */
	const lines = $derived.by((): TranscriptLine[] => {
		const out: TranscriptLine[] = [];
		docChunks.forEach((c, part) => {
			const key = hitKey(c);
			const aligned = c.alignments ?? [];
			if (aligned.length > 0) {
				aligned.forEach((a, i) => out.push({ ...a, key: `${part}:${key}:${i}` }));
				return;
			}
			const t = view.time(c);
			const text = view.body(c);
			if (t && text) out.push({ key: `${part}:${key}`, start: t.start, end: t.end, text });
		});
		return out;
	});
	const terms = $derived(new Set(queryTerms(query)));

	// Which synced view shows under the media; the element stays mounted across tabs, so playback and position never
	// reset. The tab SHOWN is derived from the pick and what the dataset offers.
	let tab = $state<'transcript' | 'speakers'>('transcript');
	const shown = $derived(shownTab(tab, panes));

	/** Jump there and play: a transcript line, a speaker's turn. */
	const seekTo = (t: number) => {
		if (!mediaEl) return;
		currentTime = t;
		paused = false;
	};

	// ── fullscreen: the whole viewer, so the transport and the captions come along ──
	let root = $state<HTMLDivElement | null>(null);
	let isFullscreen = $state(false);
	const toggleFullscreen = () => {
		if (document.fullscreenElement) void document.exitFullscreen();
		else void root?.requestFullscreen();
	};
	const onFullscreenChange = () => {
		isFullscreen = root !== null && document.fullscreenElement === root;
	};

	// ── the header: what is open, and where in it ──
	const title = $derived(hit ? view.title(hit) : '');
	const position = $derived.by((): string => {
		if (mediaKind === 'image') return pageCount > 1 ? `Page ${page + 1} of ${pageCount}` : '';
		if (!plays) return '';
		const at = fmtTime(currentTime);
		return timed.length > 1 && currentSegment >= 0
			? `Segment ${currentSegment + 1} of ${timed.length} · ${at}`
			: at;
	});
	const pictureLabel = $derived(
		pageCount > 1 ? `${title}, page ${page + 1} of ${pageCount}` : title,
	);

	// ── the keyboard, on the focused viewer ──
	// A control that takes the key itself (a button's Space, the scrubber's arrows) keeps it; the rest are the viewer's:
	// the arrows and Page Up/Down turn a document's pages (the image stage keeps the arrows while the picture
	// overflows, to pan), Space plays and pauses, the arrows step five seconds, M mutes, F goes fullscreen.
	function onkeydown(e: KeyboardEvent): void {
		if (!hit || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
		const target = e.target instanceof Element ? e.target : null;
		const control = target?.closest('button, a, input, textarea, select, [role="slider"]') ?? null;
		const key = e.key;
		if (key === 'f' || key === 'F') {
			if (control !== null && control.matches('input, textarea')) return;
			toggleFullscreen();
		} else if (mediaKind === 'image') {
			const step =
				key === 'ArrowRight' || key === 'PageDown'
					? 1
					: key === 'ArrowLeft' || key === 'PageUp'
						? -1
						: null;
			if (step !== null && pageCount > 1) goToPage(page + step);
			else if (key === 'Home' && pageCount > 1 && control === null) goToPage(0);
			else if (key === 'End' && pageCount > 1 && control === null) goToPage(pageCount - 1);
			else return;
		} else if (plays) {
			if ((key === ' ' || key === 'k') && control === null) paused = !paused;
			else if (key === 'ArrowRight' && control === null)
				currentTime = Math.min(duration || 0, currentTime + 5);
			else if (key === 'ArrowLeft' && control === null) currentTime = Math.max(0, currentTime - 5);
			else if (key === 'm' || key === 'M') muted = !muted;
			else return;
		} else return;
		e.preventDefault();
	}

	// ── metadata: the declared fields of what is shown, collapsed so it does not take the synced views' height ──
	let showMeta = $state(false);
	const metaRows = $derived.by((): [string, string][] => {
		const h = shownRow;
		if (!h) return [];
		const rows: [string, string][] = [];
		const caption = view.caption(h);
		if (caption) rows.push(['Caption', caption]);
		const t = view.time(h);
		if (t) rows.push(['Time', `${fmtTime(t.start)} → ${fmtTime(t.end)}`]);
		const dur = view.duration(h);
		if (dur != null) rows.push(['Length', fmtTime(dur)]);
		for (const m of view.metadata(h)) rows.push([m.label, m.value]);
		return rows;
	});

	/**
	 * Whenever `hit` changes, seek the element to the hit's start and play.
	 *
	 * `src` is owned by the `src={mediaSrc}` binding, never by this effect: when the next hit is in the SAME
	 * document the bound URL does not change, Svelte does not re-apply it, and an effect that had cleared the source
	 * would leave the element sourceless. The effect reads the hit's document and start, so it re-runs on a new
	 * document (the source changes, the metadata loads, the seek waits for it) and on a new part of the same, loaded
	 * document (the seek is immediate; `loadedmetadata` will not fire again).
	 */
	$effect(() => {
		mediaError = null;
		if (!hit || !mediaEl) return;

		const el = mediaEl;
		const start = view.time(hit)?.start ?? 0;
		let cancelled = false;
		const seek = () => {
			if (cancelled) return;
			try {
				el.currentTime = start;
			} catch {
				// A browser may refuse a seek before the metadata is ready; the bound currentTime corrects it.
			}
			el.play().catch(() => {});
		};
		const codes: Record<number, string> = {
			1: 'MEDIA_ERR_ABORTED',
			2: 'MEDIA_ERR_NETWORK',
			3: 'MEDIA_ERR_DECODE',
			4: 'MEDIA_ERR_SRC_NOT_SUPPORTED',
		};
		const onError = () => {
			const err = el.error;
			if (linkMayHaveExpired(err?.code)) {
				void renewSource(el).then((renewed) => {
					if (!renewed) mediaError = err ? (codes[err.code] ?? 'unknown') : 'unknown';
				});
				return;
			}
			mediaError = err ? (codes[err.code] ?? 'unknown') : 'unknown';
		};

		const offs: (() => void)[] = [];
		if (el.readyState >= 1 /* HAVE_METADATA */) {
			seek();
		} else {
			offs.push(on(el, 'loadedmetadata', seek, { once: true }));
			offs.push(on(el, 'canplay', seek, { once: true }));
		}
		offs.push(on(el, 'error', onError));

		return () => {
			cancelled = true;
			for (const off of offs) off();
			el.pause();
		};
	});
</script>

<svelte:document onfullscreenchange={onFullscreenChange} />

{#snippet fullscreenButton()}
	<Button
		variant="ghost"
		size="icon-sm"
		onclick={toggleFullscreen}
		title={isFullscreen ? 'Exit fullscreen (F)' : 'Fullscreen (F)'}
		aria-label={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
	>
		{#if isFullscreen}<Minimize2 class="size-4" />{:else}<Maximize2 class="size-4" />{/if}
	</Button>
{/snippet}

{#snippet failure(what: string, detail: string)}
	<div class="flex h-full min-h-0 items-center justify-center overflow-auto">
		<LoadFailure {what} {detail} onretry={retryAll} />
	</div>
{/snippet}

{#if !hit}
	<div
		class="text-muted-foreground flex h-full min-h-0 flex-col items-center justify-center gap-2 px-6 text-center text-sm"
	>
		<MousePointerClick class="size-5 opacity-60" aria-hidden="true" />
		<p>{idle}</p>
	</div>
{:else}
	<!-- One element holds the whole viewer, so it is the one that goes fullscreen and the one whose keys are the
	     viewer's. Focusable by script and pointer only (tabindex -1): its controls are the tab stops. In a panel too
	     short for all of it, it scrolls rather than cutting off what is below. -->
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<div
		bind:this={root}
		class="bg-card text-card-foreground @container flex h-full min-h-0 flex-col overflow-y-auto outline-none"
		role="group"
		aria-label={`Viewer: ${title}`}
		tabindex="-1"
		data-testid="viewer"
		{onkeydown}
	>
		<header class="border-border flex shrink-0 items-center gap-2 border-b py-1.5 pr-1.5 pl-3">
			<div class="min-w-0 flex-1">
				<h2 class="truncate text-sm leading-5 font-medium" {title} data-testid="viewer-title">
					{title}
				</h2>
				{#if position}
					<p
						class="text-muted-foreground truncate text-xs leading-4 tabular-nums"
						data-testid="viewer-position"
					>
						{position}
					</p>
				{/if}
			</div>
			{#if pageCount > 1}
				<nav class="flex shrink-0 items-center" aria-label="Pages">
					<Button
						variant="ghost"
						size="icon-sm"
						aria-label="Previous page"
						title="Previous page (←)"
						disabled={page <= 0}
						onclick={() => goToPage(page - 1)}
					>
						<ChevronLeft class="size-4" />
					</Button>
					<Button
						variant="ghost"
						size="icon-sm"
						aria-label="Next page"
						title="Next page (→)"
						disabled={page >= pageCount - 1}
						onclick={() => goToPage(page + 1)}
					>
						<ChevronRight class="size-4" />
					</Button>
				</nav>
			{/if}
			{@render fullscreenButton()}
		</header>

		<!-- The media: the element comes from the dataset's OWN declaration (a page-image dataset never gets a
		     <video>), and it fills the height the header, the transport and the synced views leave. -->
		{#if linkFailure !== null}
			<div class="min-h-40 flex-1">
				{@render failure("This item's media could not be loaded.", linkFailure)}
			</div>
		{:else if mediaKind === 'image'}
			<div class="bg-muted/40 relative min-h-48 flex-1" data-testid="viewer-media">
				{#if gpuFailure !== null}
					<div
						class="text-muted-foreground flex h-full flex-col items-center justify-center gap-1 px-6 text-center text-sm"
					>
						<span class="text-destructive">{gpuFailure}</span>
						<span>Pages are drawn with WebGPU. The metadata and the search work without it.</span>
					</div>
				{:else if pictureFailure !== null}
					{@render failure("This item's media could not be loaded.", pictureFailure)}
				{:else if frame?.url}
					<ImageStage
						url={frame.url}
						label={pictureLabel}
						keyboard
						fitPadding={12}
						onerror={onStageError}
					/>
				{:else}
					<div
						class="flex h-full items-center justify-center"
						role="status"
						aria-label="Loading the image"
					>
						<div class="bg-muted aspect-[3/4] h-3/4 max-h-96 animate-pulse rounded-md"></div>
					</div>
				{/if}
			</div>
		{:else if plays}
			{#if mediaKind === 'video'}
				<div
					class={[
						'bg-muted relative min-h-40',
						isFullscreen || shown === null ? 'flex-1' : 'flex-[3]',
					]}
					data-testid="viewer-media"
				>
					<!-- The frames are drawn by the shared image stage, as the labeling canvas draws them, so the picture
					     zooms and pans like a page; the element itself is hidden. -->
					<video
						bind:this={mediaEl}
						bind:currentTime
						bind:duration
						bind:paused
						bind:volume
						bind:muted
						bind:playbackRate
						preload="auto"
						crossorigin="anonymous"
						src={mediaSrc}
						class="hidden"
						onseeked={() => videoStage?.image.showVideoFrame()}
					>
						<track kind="captions" />
					</video>
					{#if mediaEl instanceof HTMLVideoElement && gpuFailure === null}
						<!-- Remounted per source: another video's frame size is another sprite. -->
						{#key mediaSrc}
							<ImageStage
								video={mediaEl}
								label={title}
								keyboard
								fitPadding={8}
								onready={onVideoStage}
								onerror={onStageError}
							/>
						{/key}
					{:else if gpuFailure !== null}
						<div
							class="text-muted-foreground flex h-full items-center justify-center px-6 text-center text-sm"
						>
							{gpuFailure}: the video's frames are drawn with WebGPU. Its sound and transcript work
							without it.
						</div>
					{/if}
					{#if isFullscreen && shown === 'transcript'}
						<div
							class="bg-card/85 text-card-foreground absolute inset-x-0 bottom-4 mx-auto w-[min(92%,60rem)] rounded-xl px-6 py-3 shadow-lg backdrop-blur"
						>
							<TranscriptView {lines} {currentTime} onseek={seekTo} {terms} variant="caption" />
						</div>
					{/if}
				</div>
			{:else}
				<!-- Audio has nothing to look at: the element is the transport's, and the transcript takes the height. -->
				<audio
					bind:this={mediaEl}
					bind:currentTime
					bind:duration
					bind:paused
					bind:volume
					bind:muted
					bind:playbackRate
					preload="metadata"
					src={mediaSrc}
					class="hidden"
				></audio>
				{#if shown === null && viewsFailure === null}
					<div
						class="bg-muted/40 text-muted-foreground flex min-h-24 flex-1 items-center justify-center"
						data-testid="viewer-media"
					>
						<AudioLines class="size-10 opacity-50" aria-hidden="true" />
					</div>
				{/if}
			{/if}
			{#if mediaError}
				<div class="border-border shrink-0 border-t">
					{@render failure("This item's media could not be played.", mediaError)}
				</div>
			{/if}
			<div class="border-border shrink-0 border-t px-3 pt-1.5">
				<TimeStrip
					{duration}
					{currentTime}
					bind:view={stripView}
					waveform={wave}
					{waveNote}
					waveHeight={mediaKind === 'audio' ? 72 : 40}
					rows={stripRows}
					playing={!paused}
					clock={() => mediaEl?.currentTime ?? 0}
					onseek={(t) => (currentTime = t)}
					label={`Time line of the ${noun}`}
				/>
			</div>
			<MediaTransport
				bind:currentTime
				bind:paused
				bind:volume
				bind:muted
				bind:playbackRate
				{duration}
				{noun}
				class="shrink-0"
			/>
		{/if}

		{#if plays && !(isFullscreen && mediaKind === 'video') && (shown !== null || viewsFailure !== null)}
			<section
				class={[
					'border-border flex min-h-32 flex-col border-t',
					mediaKind === 'video' ? 'flex-[2]' : 'flex-1',
				]}
				aria-label="Synced views"
			>
				{#if panes.tabs}
					<div class="flex shrink-0 items-center gap-1 px-2 pt-1.5" data-testid="viewer-bridge-bar">
						{#if panes.transcript}
							<Button
								variant={shown === 'transcript' ? 'secondary' : 'ghost'}
								size="xs"
								aria-pressed={shown === 'transcript'}
								data-testid="tab-transcript"
								onclick={() => (tab = 'transcript')}
							>
								Transcript
							</Button>
						{/if}
						{#if panes.speakers}
							<Button
								variant={shown === 'speakers' ? 'secondary' : 'ghost'}
								size="xs"
								aria-pressed={shown === 'speakers'}
								data-testid="tab-speakers"
								onclick={() => (tab = 'speakers')}
							>
								Speakers
							</Button>
						{/if}
					</div>
				{/if}
				{#if viewsFailure !== null && linkFailure === null}
					{@render failure(viewsFailure.what, viewsFailure.detail)}
				{:else if shown === 'speakers'}
					<div class="flex min-h-0 flex-1 flex-col" data-testid="viewer-sync-body">
						{#if diarTurns.length > 0}
							<div class="shrink-0 px-1.5">
								<DiarizationTimeline
									turns={diarTurns}
									{currentTime}
									{duration}
									segmentStart={segmentSpan?.start}
									segmentEnd={segmentSpan?.end}
									onSeek={seekTo}
									{noun}
								/>
							</div>
							<TranscriptView
								{lines}
								{currentTime}
								onseek={seekTo}
								{terms}
								class="border-border flex-1 border-t"
							/>
						{:else}
							<p class="text-muted-foreground p-3 text-xs">No speaker turns for this {noun}.</p>
						{/if}
					</div>
				{:else if shown === 'transcript'}
					<TranscriptView
						{lines}
						{currentTime}
						onseek={seekTo}
						{terms}
						empty={chunksRead?.current
							? `No transcript for this ${noun}.`
							: 'Reading the transcript…'}
						class="flex-1"
					/>
				{/if}
			</section>
		{/if}

		{#if metaRows.length && !isFullscreen}
			<div class="border-border shrink-0 border-t text-xs">
				<button
					type="button"
					onclick={() => (showMeta = !showMeta)}
					aria-expanded={showMeta}
					class="text-muted-foreground hover:bg-muted/60 focus-visible:ring-ring/50 flex w-full items-center gap-1.5 px-3 py-1.5 transition-colors outline-none focus-visible:ring-3 focus-visible:ring-inset"
				>
					<ChevronRight
						class={['size-3.5 transition-transform', showMeta && 'rotate-90']}
						aria-hidden="true"
					/>
					<span class="font-medium">Metadata</span>
					<span class="ml-auto text-[0.7rem]">{metaRows.length} fields</span>
				</button>
				{#if showMeta}
					<dl
						class="border-border grid max-h-40 grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 overflow-y-auto border-t px-3 py-2"
					>
						{#each metaRows as [label, value] (label)}
							<dt class="text-muted-foreground">{label}</dt>
							<dd class="text-foreground truncate font-medium" title={value}>{value}</dd>
						{/each}
					</dl>
				{/if}
			</div>
		{/if}
	</div>
{/if}
