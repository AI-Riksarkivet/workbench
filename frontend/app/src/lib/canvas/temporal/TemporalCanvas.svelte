<script lang="ts">
	// A recording's or a video's labeling canvas: on a video its frame on top, drawn through the shared image stage so
	// zoom, pan and every image tool work on it; under it ONE time strip (`#lib/media/time/TimeStrip.svelte`) carrying
	// the waveform, the speakers, the document's segments, the regions drawn here and each track's keyframes; then the
	// shared transport. One playhead for picture and sound, as Label Studio's.
	//
	// REGIONS. Drag on the waveform or an empty region lane to make one; drag an edge to resize it, its body to move it;
	// edges snap to the playhead and to other regions' edges while snapping is on. A new region opens the class picker.
	// Each is a `segment` row in the item's annotations, its times in seconds (`t_start`, `t_end`), saved through the same
	// door, draft and versions as a drawn shape, every change on the canvas's one undo stack.
	//
	// TRACKS (video). A box drawn on a frame is a track's first keyframe; moved at another moment it gains one there, and
	// the frames between are interpolated (`./tracks.ts`). Each track has its own lane: its presence, its keyframes as
	// diamonds (a press seeks there and selects it, a drag retimes it).
	//
	// FRAMES. A video's frame rate is not exposed by the element, so steps assume `FPS` until frames are presented while
	// it plays, whose `requestVideoFrameCallback` times give the real step. A step seeks by one frame's time and then
	// reads the presented frame's own time (`mediaTime`), so a variable-rate file pins keyframes to the frame shown, not
	// to arithmetic.
	import { onDestroy, onMount, untrack } from 'svelte';
	import { Magnet, Repeat, Rows3, ZoomIn, ZoomOut } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';
	import { Input } from '@workbench/ui/input';
	import { loadAnnotations } from '#lib/api/annotations.api.ts';
	import { documentPeaks } from '#lib/api/timeline.api.ts';
	import ImageStage, { type ImageStageContext } from '#lib/media/image/ImageStage.svelte';
	import MediaTransport from '#lib/media/time/MediaTransport.svelte';
	import TimeStrip from '#lib/media/time/TimeStrip.svelte';
	import TranscriptView from '#lib/media/time/TranscriptView.svelte';
	import {
		layout,
		type StripHit,
		type StripRow,
		type StripSpan,
		type Rgba,
	} from '#lib/media/time/strip.ts';
	import { formatClock } from '#lib/media/time/timeline.ts';
	import { wholeView, xOf, zoomAt, type TimeView } from '#lib/media/time/view.ts';
	import { rgba } from '#lib/gpu/rects.ts';
	import { ArrowDataPlugin, InteractionManager, type PixiContext } from '../engine/index.ts';
	import ClassPicker from '../ClassPicker.svelte';
	import type { AnnotatorController } from '../controller.svelte.ts';
	import { laneOf, neighbour, snapTargets, snapTo, type Region } from './regions.ts';
	import { presence } from './tracks.ts';
	import type { Part } from '../media.ts';

	let {
		controller,
		kind,
		mediaUrl,
		annotationsUrl,
		dataset,
		docId,
		parts,
		turns,
		focus,
		itemName,
		onload,
		onerror,
	}: {
		controller: AnnotatorController;
		kind: 'audio' | 'video';
		mediaUrl: string;
		annotationsUrl: string;
		dataset: string;
		docId: string;
		parts: readonly Part[];
		turns: readonly { speaker: string; start: number; end: number }[];
		/** The task's own stretch of the recording, which the view opens on. */
		focus: { start: number; end: number } | null;
		itemName: string;
		onload?: () => void;
		onerror?: (message: string, status?: number) => void;
	} = $props();

	const FPS = 25;
	const noun = $derived(kind === 'video' ? 'video' : 'recording');

	// ── the element's own state, bound ──
	let media = $state<HTMLMediaElement | null>(null);
	let currentTime = $state(0);
	let duration = $state(0);
	let paused = $state(true);
	let volume = $state(1);
	let muted = $state(false);
	let playbackRate = $state(1);
	let mediaError = $state<string | null>(null);

	let view = $state<TimeView>(wholeView(0));
	let snapping = $state(true);
	let looping = $state(false);

	// ── the waveform ──
	const peaksRead = $derived(documentPeaks({ dataset, docId }));
	const peaks = $derived(peaksRead.current);
	const waveform = $derived(peaks?.ok ? peaks.data : null);
	const waveNote = $derived(
		peaks === undefined
			? 'Reading the waveform…'
			: !peaks.ok
				? `The waveform could not be read: ${peaks.detail}`
				: !peaks.data.audio
					? `This ${noun} has no sound: the time line has no waveform.`
					: peaks.data.cut
						? null
						: null,
	);

	// ── the annotations: a recording's rows are read here; a video's with its frame, through the stage ──
	let alive = true;
	/** What this canvas attached the controller to: a canvas mounted again (the page narrowing moves it) attaches its
	 *  own before this one is destroyed, and this one must not take that away. */
	let attachedCtx: PixiContext | null = null;
	let attachedTable: unknown = null;
	onDestroy(() => {
		alive = false;
		if (controller.timeKeys === timeKey) controller.timeKeys = null;
		if (controller.timeGesture === holdsRegion) controller.timeGesture = null;
		if (kind === 'video' && controller.ctx !== null && controller.ctx === attachedCtx) {
			controller.setTrackMode(false);
			controller.detach();
		} else if (kind === 'audio' && controller.table !== null && controller.table === attachedTable)
			controller.detachTable();
	});

	onMount(() => {
		if (kind !== 'audio') return;
		void (async () => {
			const loaded = await loadAnnotations(annotationsUrl);
			if (!alive) return;
			if (!loaded.ok) {
				onerror?.(loaded.detail, loaded.status);
				return;
			}
			controller.attachTable(loaded.data.table, annotationsUrl, loaded.data.version);
			attachedTable = controller.table;
			onload?.();
		})();
	});

	// The view opens on the task's own part, with a little of what is around it.
	let opened = $state(false);
	$effect(() => {
		if (untrack(() => opened) || !(duration > 0)) return;
		opened = true;
		const f = untrack(() => focus);
		if (f) {
			const pad = Math.max(0.5, (f.end - f.start) * 0.25);
			view = { start: Math.max(0, f.start - pad), end: Math.min(duration, f.end + pad) };
			if (media) media.currentTime = f.start;
		}
	});

	// ── a video's frame, through the image stage, and its layer of shapes ──
	let stage: ImageStageContext | null = null;
	let interaction: InteractionManager | undefined;
	let arrow: ArrowDataPlugin | undefined;
	let fps = $state(FPS);

	function onstage(ready: ImageStageContext): () => void {
		stage = ready;
		const { app, image } = ready;
		const shapes = new ArrowDataPlugin(app);
		const im = new InteractionManager(app, shapes);
		arrow = shapes;
		interaction = im;
		shapes.setViewport(image.getViewportBounds());
		const ctx: PixiContext = { app, plugins: { image, arrow: shapes, interaction: im } };
		im.setImageSource(null);
		void (async () => {
			const loaded = await loadAnnotations(annotationsUrl);
			if (!alive) return;
			if (!loaded.ok) {
				onerror?.(loaded.detail, loaded.status);
				return;
			}
			shapes.load(loaded.data.table);
			shapes.sync();
			controller.frameTolerance = 0.5 / fps;
			controller.attach(ctx, loaded.data.table, annotationsUrl, loaded.data.version);
			attachedCtx = ctx;
			controller.setTrackMode(true);
			controller.setTimeCursor(media?.currentTime ?? 0);
			image.showVideoFrame();
			onload?.();
		})();
		watchFrames();
		return () => {
			stage = null;
			im.destroy();
			shapes.destroy();
			interaction = undefined;
			arrow = undefined;
		};
	}

	/** Each presented frame is drawn on the stage and becomes the playhead the shapes are drawn at; the gap between two
	 *  frames presented while playing is the frame step. */
	let lastFrame: { mediaTime: number; frames: number } | null = null;
	function watchFrames(): void {
		const video = media as HTMLVideoElement | null;
		if (!video || !('requestVideoFrameCallback' in video)) return;
		const onframe = (_now: number, meta: VideoFrameCallbackMetadata) => {
			if (!alive || !stage) return;
			stage.image.showVideoFrame();
			controller.setTimeCursor(meta.mediaTime);
			if (lastFrame && meta.presentedFrames === lastFrame.frames + 1) {
				const step = meta.mediaTime - lastFrame.mediaTime;
				if (step > 0.004 && step < 0.25) {
					fps = Math.round(1 / step);
					controller.frameTolerance = 0.5 * step;
				}
			}
			lastFrame = { mediaTime: meta.mediaTime, frames: meta.presentedFrames };
			video.requestVideoFrameCallback(onframe);
		};
		video.requestVideoFrameCallback(onframe);
	}

	/** A seek lands: the frame it shows is drawn, and the playhead the shapes are drawn at is the element's (a frame
	 *  presented after it refines it to that frame's own time). A paused seek presents no frame to watch on every
	 *  browser, so this does not wait for one. */
	function onseeked(): void {
		stage?.image.showVideoFrame();
		controller.setTimeCursor(media?.currentTime ?? 0);
	}

	// On a recording the playhead the regions snap to is the element's.
	$effect(() => {
		if (kind === 'audio') controller.setTimeCursor(currentTime);
	});

	// ── rows on the strip ──
	const colourCache = new Map<string, Rgba>();
	const colour = (css: string, alpha = 1): Rgba => {
		const key = `${css}/${alpha}`;
		let c = colourCache.get(key);
		if (!c) {
			c = rgba(css, alpha);
			colourCache.set(key, c);
		}
		return c;
	};
	const SPEAKER_COLOURS = ['#7c3aed', '#0d9488', '#d97706', '#db2777', '#2563eb', '#65a30d'];

	const regions = $derived<Region[]>(
		controller.rows
			.filter((r) => r.shape === 'segment' && r.tStart !== null && r.tEnd !== null)
			.map((r) => ({ index: r.index, start: r.tStart ?? 0, end: r.tEnd ?? 0 })),
	);

	/** A region being drawn, resized or moved, before it is let go. */
	let drag = $state<
		| { mode: 'create'; from: number; start: number; end: number }
		| { mode: 'start' | 'end' | 'move'; index: number; offset: number; start: number; end: number }
		| { mode: 'keyframe'; index: number; at: number }
		| null
	>(null);

	const shown = $derived.by<Region[]>(() => {
		const d = drag;
		if (!d || d.mode === 'keyframe') return regions;
		if (d.mode === 'create') return [...regions, { index: -1, start: d.start, end: d.end }];
		return regions.map((r) => (r.index === d.index ? { ...r, start: d.start, end: d.end } : r));
	});
	const lanes = $derived(laneOf(shown));
	const laneCount = $derived(Math.max(1, ...[...lanes.values()].map((l) => l + 1)));
	const labelOf = (index: number): string =>
		controller.rows.find((r) => r.index === index)?.label ?? '';

	const stripRows = $derived.by<StripRow[]>(() => {
		const out: StripRow[] = [];
		const speakers = [...new Set(turns.map((t) => t.speaker))];
		speakers.forEach((speaker, i) =>
			out.push({
				id: `speaker:${speaker}`,
				label: speaker,
				height: 16,
				spans: turns
					.filter((t) => t.speaker === speaker)
					.map((t, j) => ({
						key: `${speaker}:${j}`,
						start: t.start,
						end: t.end,
						colour: colour(SPEAKER_COLOURS[i % SPEAKER_COLOURS.length] ?? '#888', 0.9),
					})),
			}),
		);
		if (parts.length > 0)
			out.push({
				id: 'parts',
				label: kind === 'video' ? 'Shots' : 'Segments',
				height: 18,
				spans: parts.map((p, i) => ({
					key: `part:${i}`,
					start: p.start,
					end: p.end,
					colour: colour('#94a3b8', p.own ? 1 : 0.6),
					selected: p.own,
				})),
			});
		for (const track of controller.tracks) {
			const selected = track.keyframes.some((k) => k.index === controller.selectedIndex);
			const c = colour(controller.classColour.get(track.label) ?? '#64748b');
			out.push({
				id: `track:${track.group}`,
				label: track.label || 'Track',
				height: 20,
				spans: presence(track, duration).map((p, i) => ({
					key: `${track.group}:${i}`,
					start: p.start,
					end: p.end,
					colour: c,
					selected,
				})),
				markers: track.keyframes.map((k) => ({
					key: k.id,
					t: drag?.mode === 'keyframe' && drag.index === k.index ? drag.at : k.t,
					colour: k.outside ? colour('#64748b') : c,
					selected: k.index === controller.selectedIndex,
				})),
			});
		}
		for (let lane = 0; lane < laneCount; lane += 1)
			out.push({
				id: `regions:${lane}`,
				label: lane === 0 ? 'Regions' : '',
				height: 24,
				spans: shown
					.filter((r) => lanes.get(r.index) === lane)
					.map((r): StripSpan => {
						const label = r.index >= 0 ? labelOf(r.index) : '';
						return {
							key: `region:${r.index}`,
							start: r.start,
							end: r.end,
							colour: colour(controller.classColour.get(label) ?? '#64748b'),
							label,
							selected: r.index === controller.selectedIndex,
							hollow: !label,
						};
					}),
			});
		return out;
	});

	/** Whether this task's regions of time can take a class: an ontology naming no class, or one a segment may take. */
	const segmentsAllowed = $derived(
		controller.classDefs.length === 0 ||
			controller.classDefs.some((c) => c.tools.length === 0 || c.tools.includes('segment')),
	);

	// ── pointer on the strip ──
	/** Pixels on the strip's time axis as seconds at the view as it is: the axis is the strip less its names' gutter. */
	const pxToSeconds = (px: number): number => {
		const axis =
			stripBox?.querySelector<HTMLElement>('[data-testid="time-strip"]')?.clientWidth ?? 0;
		return (px / Math.max(1, axis)) * (view.end - view.start);
	};
	const snap = (t: number, moving: number | null): number =>
		snapping ? snapTo(t, snapTargets(regions, moving, currentTime), pxToSeconds(8)) : t;
	const clampT = (t: number): number => Math.min(Math.max(0, t), duration || 0);

	function onpointer(kindOf: 'down' | 'move' | 'up', hit: StripHit, e: PointerEvent): boolean {
		if (kindOf === 'down') {
			const t = clampT(hit.t);
			if (hit.area === 'row' && hit.marker) {
				const index = Number(controller.rows.find((r) => r.id === hit.marker?.key)?.index);
				if (Number.isFinite(index)) {
					controller.select(index);
					seek(hit.marker.t);
					if (controller.canEditTime) drag = { mode: 'keyframe', index, at: hit.marker.t };
				}
				return true;
			}
			if (hit.area === 'row' && hit.row.startsWith('track:') && hit.span) {
				const group = hit.row.slice('track:'.length);
				const track = controller.tracks.find((tr) => tr.group === group);
				const kf = track?.keyframes.find((k) => k.t <= t) ?? track?.keyframes[0];
				if (kf) controller.select(kf.index);
				seek(t);
				return true;
			}
			if (hit.area === 'row' && hit.row.startsWith('regions:') && hit.span) {
				const index = Number(hit.span.key.slice('region:'.length));
				controller.select(index);
				if (!controller.canEditTime) return true;
				const r = regions.find((x) => x.index === index);
				if (!r) return true;
				drag = {
					mode: hit.edge ?? 'move',
					index,
					offset: t - r.start,
					start: r.start,
					end: r.end,
				};
				return true;
			}
			const onRegionLane = hit.area === 'row' && hit.row.startsWith('regions:');
			if (
				(hit.area === 'wave' || onRegionLane) &&
				controller.canEditTime &&
				segmentsAllowed &&
				!e.shiftKey
			) {
				const from = snap(t, null);
				drag = { mode: 'create', from, start: from, end: from };
				return true;
			}
			return false;
		}
		const d = drag;
		if (!d) return false;
		const t = clampT(hit.t);
		if (kindOf === 'move') {
			if (d.mode === 'create') {
				const at = snap(t, null);
				drag = { ...d, start: Math.min(d.from, at), end: Math.max(d.from, at) };
			} else if (d.mode === 'start')
				drag = { ...d, start: Math.min(snap(t, d.index), d.end - 0.01) };
			else if (d.mode === 'end') drag = { ...d, end: Math.max(snap(t, d.index), d.start + 0.01) };
			else if (d.mode === 'move') {
				const length = d.end - d.start;
				let start = clampT(t - d.offset);
				const snapped = snap(start, d.index);
				const snappedEnd = snap(start + length, d.index);
				if (snapped !== start) start = snapped;
				else if (snappedEnd !== start + length) start = snappedEnd - length;
				start = Math.min(Math.max(0, start), Math.max(0, duration - length));
				drag = { ...d, start, end: start + length };
			} else if (d.mode === 'keyframe') drag = { ...d, at: t };
			return true;
		}
		drag = null;
		if (d.mode === 'create') {
			// A press with no drag is a click: it seeks rather than making a region nobody can see.
			if (d.end - d.start < pxToSeconds(4)) seek(d.from);
			else controller.addSegment(d.start, d.end);
		} else if (d.mode === 'keyframe') controller.retimeKeyframe(d.index, d.at);
		else controller.updateSegmentTime(d.index, d.start, d.end);
		return true;
	}

	// ── playback ──
	function seek(t: number): void {
		if (!media) return;
		media.currentTime = clampT(t);
	}

	const selectedRegion = $derived(
		regions.find((r) => r.index === controller.selectedIndex) ?? null,
	);

	function playRegion(): void {
		const r = selectedRegion;
		if (!r || !media) return;
		media.currentTime = r.start;
		void media.play();
	}

	// While a region plays, it stops at its end, or starts again with looping on.
	$effect(() => {
		const r = selectedRegion;
		if (paused || !r || !media) return;
		const el = media;
		let frame = 0;
		const watch = () => {
			if (el.currentTime >= r.end) {
				if (looping) el.currentTime = r.start;
				else if (el.currentTime - r.end < 0.25) el.pause();
			}
			frame = requestAnimationFrame(watch);
		};
		frame = requestAnimationFrame(watch);
		return () => cancelAnimationFrame(frame);
	});

	/** One frame's time at the rate measured (or assumed). */
	const frame = $derived(1 / fps);

	function stepFrames(n: number): void {
		if (!media) return;
		media.pause();
		seek((media.currentTime || 0) + n * frame);
	}

	function nudge(edge: 'start' | 'end', delta: number): void {
		const r = selectedRegion;
		if (!r) return;
		const start = edge === 'start' ? clampT(Math.min(r.start + delta, r.end - 0.01)) : r.start;
		const end = edge === 'end' ? clampT(Math.max(r.end + delta, r.start + 0.01)) : r.end;
		controller.updateSegmentTime(r.index, start, end);
	}

	// A region marked from the keyboard: `[` at its start, `]` at its end.
	let markedStart = $state<number | null>(null);

	/** Whether a region is held under the pointer: the controller's autosave waits for its release. */
	const holdsRegion = (): boolean => drag !== null;

	/** The time keys (`time-keys.ts`), from the strip or, through the page's handler, from anywhere on the item (the
	 *  frame a box was just drawn on included). True when the key was one of them. */
	function timeKey(e: KeyboardEvent): boolean {
		const target = e.target as HTMLElement | null;
		if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return false;
		// An open class picker takes the keys (the page's handler hands them to it).
		if (e.ctrlKey || e.metaKey || controller.picker) return false;
		const t = media?.currentTime ?? 0;
		const step = kind === 'video' ? frame : 0.01;
		const handled = (): void => {
			e.preventDefault();
			e.stopPropagation();
		};
		switch (e.key) {
			case ' ':
				handled();
				if (e.shiftKey) playRegion();
				else if (media) void (media.paused ? media.play() : media.pause());
				return true;
			case 'ArrowLeft':
			case 'ArrowRight': {
				handled();
				const sign = e.key === 'ArrowLeft' ? -1 : 1;
				if (kind === 'video' && !e.shiftKey && !e.altKey) stepFrames(sign);
				else seek(t + sign * (e.shiftKey ? 1 : e.altKey ? 0.01 : 0.1));
				return true;
			}
			case '[':
				handled();
				if (selectedRegion) nudge('start', t - selectedRegion.start);
				else markedStart = t;
				return true;
			case ']':
				handled();
				if (selectedRegion) nudge('end', t - selectedRegion.end);
				else if (markedStart !== null && segmentsAllowed) {
					controller.addSegment(Math.min(markedStart, t), Math.max(markedStart, t));
					markedStart = null;
				}
				return true;
			case ',':
			case '.':
				handled();
				nudge('start', (e.key === ',' ? -1 : 1) * (e.altKey ? 0.1 : step));
				return true;
			case '<':
			case '>':
				handled();
				nudge('end', (e.key === '<' ? -1 : 1) * (e.altKey ? 0.1 : step));
				return true;
			case 'n':
			case 'p': {
				handled();
				const next = neighbour(regions, controller.selectedIndex, e.key === 'n' ? 1 : -1);
				if (next) {
					controller.select(next.index);
					seek(next.start);
					if (next.start < view.start || next.end > view.end)
						view = zoomAt(view, next.start, 1, duration);
				}
				return true;
			}
			case 'l':
				handled();
				looping = !looping;
				return true;
			case 's':
				handled();
				snapping = !snapping;
				return true;
			case 'o':
				if (kind !== 'video') return false;
				handled();
				controller.toggleOutside();
				return true;
			case 't':
				if (kind !== 'video') return false;
				handled();
				controller.copyToNextFrame(frame);
				stepFrames(1);
				return true;
			case '+':
			case '=':
				handled();
				view = zoomAt(view, t, 1.5, duration);
				return true;
			case '-':
				handled();
				view = zoomAt(view, t, 1 / 1.5, duration);
				return true;
			case '0':
				handled();
				view = wholeView(duration);
				return true;
		}
		return false;
	}

	function onkeydown(e: KeyboardEvent): void {
		timeKey(e);
	}

	// ── what the selected region says ──
	const selectedRow = $derived(
		selectedRegion ? (controller.rows.find((r) => r.index === selectedRegion.index) ?? null) : null,
	);

	/** Start from the document's own segments: each becomes an unlabelled region carrying its text, to correct. */
	function importParts(): void {
		for (const p of parts)
			controller.addSegment(p.start, p.end, {
				text: p.text,
				label: '',
				status: 'prediction',
				source: 'dataset',
			});
	}

	// The class picker of a region opens over it on the strip.
	let stripBox = $state<HTMLDivElement | null>(null);
	/** A recording has nothing else to look at, so its waveform takes the height; under a video it is a lane. */
	const waveHeight = $derived(kind === 'audio' ? 144 : 56);
	const rowTops = $derived(
		new Map(layout(stripRows, waveHeight).map(({ top, row }) => [row.id, top])),
	);
	const pickerAnchor = $derived.by(() => {
		const open = controller.picker;
		if (!open || !stripBox || !regionEl) return null;
		const r = regions.find((x) => x.index === open.index);
		if (!r) return null;
		const strip = stripBox.querySelector('[data-testid="time-strip"]');
		if (!(strip instanceof HTMLElement)) return null;
		const at = strip.getBoundingClientRect();
		const origin = regionEl.getBoundingClientRect();
		const top = rowTops.get(`regions:${lanes.get(r.index) ?? 0}`) ?? 0;
		const x0 = xOf(view, r.start, strip.clientWidth);
		const x1 = xOf(view, r.end, strip.clientWidth);
		return {
			x: at.left - origin.left + x0,
			y: at.top - origin.top + top,
			width: Math.max(4, x1 - x0),
			height: 24,
		};
	});
	let regionEl = $state<HTMLDivElement | null>(null);
	const refocus = (): void => regionEl?.focus();

	onMount(() => {
		regionEl?.focus();
		controller.timeKeys = timeKey;
		controller.timeGesture = holdsRegion;
	});
</script>

<!-- The temporal canvas: one focusable region whose keys play, step and edit regions; the strip and the transport are
     its controls. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
	bind:this={regionEl}
	class="focus-visible:ring-ring relative flex h-full min-h-0 w-full flex-col outline-none focus-visible:ring-2 focus-visible:ring-inset"
	role="application"
	aria-label={`Labeling canvas: ${itemName}`}
	aria-roledescription={kind === 'video' ? 'video labeling canvas' : 'recording labeling canvas'}
	aria-describedby="temporal-keys"
	tabindex="0"
	data-testid="temporal-canvas"
	data-kind={kind}
	data-ready={controller.table !== null && opened ? 'true' : 'false'}
	{onkeydown}
>
	<p id="temporal-keys" class="sr-only">
		Space plays, Shift+Space plays the selected region. Arrows move the playhead{kind === 'video'
			? ' a frame'
			: ' 100 ms'}, Shift a second. Drag on the waveform to make a region; [ and ] set its start and
		end at the playhead; N and P go to the next and previous region; L loops; S snaps.{kind === 'video'
			? ' T copies the selected track to the next frame; O marks it outside the picture.'
			: ''}
	</p>
	{#if kind === 'video'}
		<!-- The frame, drawn by the shared image stage from the hidden element. -->
		<div class="bg-muted/40 relative min-h-40 flex-1" data-testid="video-frame">
			<video
				bind:this={media}
				bind:currentTime
				bind:duration
				bind:paused
				bind:volume
				bind:muted
				bind:playbackRate
				src={mediaUrl}
				crossorigin="anonymous"
				playsinline
				preload="auto"
				class="hidden"
				{onseeked}
				onerror={() => (mediaError = `The ${noun} could not be played.`)}
			>
				<track kind="captions" />
			</video>
			{#if media}
				<ImageStage
					video={media as HTMLVideoElement}
					label={itemName}
					controls
					fitPadding={8}
					canPan={() => interaction?.currentTool === 'pan'}
					onready={onstage}
					onerror={(failure) => onerror?.(failure.message)}
					onviewport={(bounds) => {
						arrow?.setViewport(bounds);
						interaction?.updateHandles();
					}}
				/>
			{/if}
		</div>
	{:else}
		<audio
			bind:this={media}
			bind:currentTime
			bind:duration
			bind:paused
			bind:volume
			bind:muted
			bind:playbackRate
			src={mediaUrl}
			preload="auto"
			class="hidden"
			{onseeked}
			onerror={() => (mediaError = `The ${noun} could not be played.`)}
		></audio>
	{/if}

	<div
		class={['flex shrink-0 flex-col', kind === 'video' ? 'border-border border-t' : 'pt-12']}
		bind:this={stripBox}
	>
		<div class="flex items-center gap-1 px-2 py-1 text-xs">
			<span class="text-muted-foreground min-w-0 flex-1 truncate" data-testid="temporal-status">
				{#if markedStart !== null}Region from {formatClock(markedStart)}: press ] at its end.
				{:else if selectedRegion}Region {formatClock(selectedRegion.start)}–{formatClock(selectedRegion.end)}{selectedRow?.label
						? ` · ${selectedRow.label}`
						: ''}
				{:else if controller.canEditTime && kind === 'video'}{segmentsAllowed
						? 'Draw a box on the frame, or drag on the waveform to mark a region.'
						: 'Draw a box on the frame: it follows its object through the keyframes you set.'}
				{:else if controller.canEditTime}Drag on the waveform to mark a region.
				{:else}{regions.length} region{regions.length === 1 ? '' : 's'}{/if}
				{#if controller.saveStatus}<span data-testid="temporal-save"> · {controller.saveStatus}</span>{/if}
			</span>
			{#if controller.canEditTime && segmentsAllowed && regions.length === 0 && parts.length > 0}
				<Button variant="outline" size="xs" onclick={importParts} data-testid="import-parts">
					<Rows3 class="size-3.5" /> Start from the {parts.length}
					{kind === 'video' ? 'shots' : 'segments'}
				</Button>
			{/if}
			<Button
				variant={snapping ? 'secondary' : 'ghost'}
				size="xs"
				aria-pressed={snapping}
				title="Snap edges to the playhead and to other regions (S)"
				onclick={() => (snapping = !snapping)}
			>
				<Magnet class="size-3.5" /> Snap
			</Button>
			<Button
				variant={looping ? 'secondary' : 'ghost'}
				size="xs"
				aria-pressed={looping}
				title="Loop the selected region while it plays (L)"
				onclick={() => (looping = !looping)}
			>
				<Repeat class="size-3.5" /> Loop
			</Button>
			<Button
				variant="ghost"
				size="icon-xs"
				aria-label="Zoom in on the time line"
				title="Zoom in (+, or Ctrl with the wheel)"
				onclick={() => (view = zoomAt(view, currentTime, 1.5, duration))}
				><ZoomIn class="size-3.5" /></Button
			>
			<Button
				variant="ghost"
				size="icon-xs"
				aria-label="Zoom out on the time line"
				title="Zoom out (−)"
				onclick={() => (view = zoomAt(view, currentTime, 1 / 1.5, duration))}
				><ZoomOut class="size-3.5" /></Button
			>
		</div>
		<div class="px-2">
			<TimeStrip
				{duration}
				{currentTime}
				bind:view
				{waveform}
				{waveNote}
				rows={stripRows}
				{waveHeight}
				playing={!paused}
				clock={() => media?.currentTime ?? 0}
				onseek={seek}
				{onpointer}
				label={`Time line of the ${noun}`}
			>
				{#snippet overlay({ view: v, width, top })}
					{#each shown as r (r.index)}
						{@const label = r.index >= 0 ? labelOf(r.index) : ''}
						{#if label && r.end > v.start && r.start < v.end}
							<span
								class="text-foreground pointer-events-none absolute truncate px-1 text-[11px] leading-5 font-medium"
								style:left={`${Math.max(0, xOf(v, r.start, width))}px`}
								style:width={`${Math.max(0, xOf(v, Math.min(r.end, v.end), width) - Math.max(0, xOf(v, r.start, width)))}px`}
								style:top={`${top(`regions:${lanes.get(r.index) ?? 0}`) + 2}px`}
								data-testid="region-label">{label}</span
							>
						{/if}
					{/each}
				{/snippet}
			</TimeStrip>
		</div>
		{#if waveform?.cut}
			<p class="text-muted-foreground px-3 text-xs" data-testid="wave-cut">
				The waveform covers {formatClock(waveform.covered)} of {formatClock(waveform.duration)}: its
				read stopped at the viewer's {waveform.cut === 'time'
					? 'time'
					: waveform.cut === 'bytes'
						? 'byte'
						: 'length'} bound.
			</p>
		{/if}
		{#if selectedRow && controller.offersTranscription(selectedRow.label) && controller.classDefs.length > 0}
			<label class="flex items-center gap-2 px-3 py-1 text-xs" data-testid="region-transcription">
				<span class="text-muted-foreground shrink-0">Transcription</span>
				<Input
					class="h-7"
					disabled={controller.readOnly !== null}
					value={selectedRow.text}
					placeholder="What is said in this region"
					oninput={(e) => controller.updateField(selectedRow.index, 'text', e.currentTarget.value)}
				/>
			</label>
		{/if}
		{#if mediaError}
			<p class="text-destructive px-3 text-xs" role="alert">{mediaError}</p>
		{/if}
		<MediaTransport
			bind:currentTime
			bind:paused
			bind:volume
			bind:muted
			bind:playbackRate
			{duration}
			{noun}
			class="border-border border-t"
		/>
	</div>
	{#if kind === 'audio'}
		<!-- What the recording says, part by part: a line seeks there, the item's own part is marked. -->
		<TranscriptView
			lines={parts.map((p, i) => ({ key: `part:${i}`, start: p.start, end: p.end, text: p.text }))}
			{currentTime}
			onseek={seek}
			class="border-border min-h-0 flex-1 border-t"
			empty="The dataset holds no transcript for this recording."
		/>
	{/if}
	{#if pickerAnchor}
		<ClassPicker {controller} anchor={pickerAnchor} onclose={refocus} />
	{/if}
</div>
