<script lang="ts">
	// A recording's time strip: a ruler, its waveform and rows of spans and markers (speakers, segments, a labeling
	// layer's regions, a track's keyframes), all on one time axis, drawn with WebGPU (`#lib/gpu/rects.ts`). Shared by the
	// dataset's player, where it is read-only, and the labeling canvas, which takes its pointer through `onpointer`.
	//
	// The view zooms and scrolls: Ctrl or Cmd with the wheel (a trackpad's pinch is the same event) zooms about the
	// pointer, Shift with the wheel or a sideways swipe scrolls, and on the focused strip + and − zoom about the playhead
	// and 0 shows the whole recording. A press on the ruler or the waveform seeks, unless `onpointer` takes it. While
	// playing, the view follows the playhead.
	import { onMount, untrack, type Snippet } from 'svelte';
	import { appearance } from '@workbench/ui/appearance';
	import { RectRenderer, rgba } from '#lib/gpu/rects.ts';
	import { formatClock } from './timeline.ts';
	import {
		hitTest,
		layout,
		RULER,
		stripHeight,
		stripRects,
		ticks,
		WAVE,
		type StripColours,
		type StripHit,
		type StripRow,
	} from './strip.ts';
	import { follow, panBy, wholeView, xOf, zoomAt, type TimeView } from './view.ts';
	import type { Waveform } from './waveform/peaks.ts';

	let {
		duration,
		currentTime,
		view = $bindable(wholeView(0)),
		waveform,
		waveNote = null,
		waveHeight = WAVE,
		rows = [],
		playing = false,
		clock,
		onseek,
		onpointer,
		label = 'Time line',
		overlay,
		class: className,
	}: {
		duration: number;
		currentTime: number;
		view?: TimeView;
		/** The waveform, once read; null while it is read or when there is none. */
		waveform: Waveform | null;
		/** What to say in the waveform's lane instead of drawing it (loading, no sound, a failure), or null. */
		waveNote?: string | null;
		/** The waveform lane's height in pixels; 0 draws none. */
		waveHeight?: number;
		rows?: readonly StripRow[];
		playing?: boolean;
		/** The playhead as the element has it now, read each frame while it plays. */
		clock?: () => number;
		onseek?: (t: number) => void;
		/** A press, a move with it held, and its release, with what they are over. True when the caller took the press, so
		 *  it does not seek. */
		onpointer?: (
			kind: 'down' | 'move' | 'up',
			hit: StripHit,
			event: PointerEvent,
		) => boolean | void;
		label?: string;
		/** Drawn over the strip, inside its box: the caller's DOM over the canvas (a region's handles, its class). */
		overlay?: Snippet<[{ view: TimeView; width: number; top: (row: string) => number }]>;
		class?: string;
	} = $props();

	let host = $state<HTMLDivElement | null>(null);
	let canvas = $state<HTMLCanvasElement | null>(null);
	/** The width of the rows' names, beside the axis: the spans start at its right, so a name never covers one. */
	const GUTTER = 68;
	let width = $state(0);
	let renderer = $state.raw<RectRenderer | null>(null);
	let failure = $state<string | null>(null);
	let colours: StripColours | null = null;

	const wave = $derived(waveHeight);
	const height = $derived(stripHeight(rows, wave));
	const known = $derived(Number.isFinite(duration) && duration > 0);

	function readColours(): StripColours {
		const style = getComputedStyle(document.documentElement);
		const token = (name: string, fallback: string) =>
			style.getPropertyValue(name).trim() || fallback;
		return {
			wave: rgba(token('--primary', '#3b82f6'), 0.85),
			waveCut: rgba(token('--muted-foreground', '#888'), 0.18),
			lane: rgba(token('--border', '#ddd'), 1),
			tick: rgba(token('--muted-foreground', '#888'), 0.7),
			playhead: rgba(token('--destructive', '#e11d48'), 1),
			selection: rgba(token('--ring', '#3b82f6'), 1),
		};
	}

	// The colours are the page's tokens: read again whenever the theme, mode or accent changes.
	$effect(() => {
		void appearance.resolved;
		colours = readColours();
		untrack(draw);
	});

	onMount(() => {
		let alive = true;
		void (async () => {
			if (!canvas) return;
			const made = await RectRenderer.create(canvas);
			if (!alive) {
				if (made instanceof RectRenderer) made.destroy();
				return;
			}
			if (made instanceof RectRenderer) renderer = made;
			else failure = made.reason;
		})();
		return () => {
			alive = false;
			renderer?.destroy();
			renderer = null;
		};
	});

	// The element's time, and every frame while it plays (written by the follow loop below).
	let playhead = $derived(currentTime);

	function draw(): void {
		const r = renderer;
		if (!r || !canvas || width <= 0) return;
		colours ??= readColours();
		const dpr = window.devicePixelRatio || 1;
		const w = Math.round(width * dpr);
		const h = Math.round(height * dpr);
		if (canvas.width !== w || canvas.height !== h) {
			canvas.width = w;
			canvas.height = h;
		}
		const { rects, count } = stripRects({ view, width, rows, waveform, wave, playhead, colours });
		r.draw(rects, count, width, height);
	}

	// Drawn again whenever what it shows changes: the view, the rows, the waveform, the playhead, the size.
	$effect(() => {
		void [view, rows, waveform, wave, playhead, width, height, renderer];
		draw();
	});

	// While it plays the playhead is read every frame, and the view moves on when the playhead leaves it.
	$effect(() => {
		if (!playing || !clock) return;
		const read = clock;
		let frame = 0;
		const tick = () => {
			const t = read();
			playhead = t;
			const next = follow(view, t, duration, 0.05);
			if (next !== view) view = next;
			frame = requestAnimationFrame(tick);
		};
		frame = requestAnimationFrame(tick);
		return () => cancelAnimationFrame(frame);
	});

	// A recording whose length becomes known (or changes) is shown whole, unless its holder has already chosen a stretch
	// of it to open on.
	let shownFor = -1;
	$effect(() => {
		if (!known || duration === shownFor) return;
		shownFor = duration;
		const current = untrack(() => view);
		if (
			current.end > duration ||
			current.end - current.start <= 0 ||
			(current.start === 0 && current.end === 1)
		)
			view = wholeView(duration);
	});

	function point(e: PointerEvent | WheelEvent): { x: number; y: number } {
		const box = host?.getBoundingClientRect();
		return { x: e.clientX - (box?.left ?? 0), y: e.clientY - (box?.top ?? 0) };
	}

	function hit(e: PointerEvent): StripHit {
		const { x, y } = point(e);
		return hitTest({ view, width, rows, wave, x, y });
	}

	let seeking = false;
	let pressed = false;
	function onpointerdown(e: PointerEvent): void {
		if (!known || e.button !== 0) return;
		const h = hit(e);
		pressed = true;
		host?.setPointerCapture(e.pointerId);
		if (onpointer?.('down', h, e)) return;
		if (h.area === 'ruler' || h.area === 'wave' || (h.area === 'row' && !h.span && !h.marker)) {
			seeking = true;
			onseek?.(Math.min(duration, Math.max(0, h.t)));
		}
	}
	function onpointermove(e: PointerEvent): void {
		if (!pressed) return;
		const h = hit(e);
		if (seeking) onseek?.(Math.min(duration, Math.max(0, h.t)));
		else onpointer?.('move', h, e);
	}
	function onpointerup(e: PointerEvent): void {
		if (!pressed) return;
		pressed = false;
		if (host?.hasPointerCapture(e.pointerId)) host.releasePointerCapture(e.pointerId);
		if (seeking) seeking = false;
		else onpointer?.('up', hit(e), e);
	}

	function onwheel(e: WheelEvent): void {
		if (!known) return;
		const { x } = point(e);
		const at = view.start + (x / Math.max(1, width)) * (view.end - view.start);
		if (e.ctrlKey || e.metaKey) {
			e.preventDefault();
			view = zoomAt(view, at, Math.exp(-Math.max(-60, Math.min(60, e.deltaY)) * 0.01), duration);
		} else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
			e.preventDefault();
			const delta = e.shiftKey && e.deltaX === 0 ? e.deltaY : e.deltaX;
			view = panBy(view, (delta / Math.max(1, width)) * (view.end - view.start), duration);
		}
	}

	function onkeydown(e: KeyboardEvent): void {
		if (!known || e.ctrlKey || e.metaKey || e.altKey) return;
		if (e.key === '+' || e.key === '=') view = zoomAt(view, playhead, 1.5, duration);
		else if (e.key === '-' || e.key === '_') view = zoomAt(view, playhead, 1 / 1.5, duration);
		else if (e.key === '0') view = wholeView(duration);
		else return;
		e.preventDefault();
		e.stopPropagation();
	}

	/** A tick's time as a clock reads it, with tenths when the view is short enough to need them. */
	function tickLabel(t: number, v: TimeView): string {
		const whole = Math.floor(t + 1e-6);
		const tenths = Math.round((t - whole) * 10);
		return v.end - v.start < 20 && tenths > 0
			? `${formatClock(whole)}.${tenths}`
			: formatClock(whole);
	}

	const tickLabels = $derived(width > 0 && known ? ticks(view, width) : []);
	const rowTops = $derived(new Map(layout(rows, wave).map(({ top, row }) => [row.id, top])));
	const zoomed = $derived(known && view.end - view.start < duration - 1e-6);
</script>

<!-- The rows' names in a gutter of their own, which no row draws under, beside the time axis; there from the first
     frame, so the axis does not narrow under a pointer when the rows arrive. -->
<div class={['flex w-full', className]} style:height={`${height}px`}>
	<div class="relative shrink-0" style:width={`${GUTTER}px`} data-testid="strip-gutter">
		{#each rows as row (row.id)}
			<span
				class="text-muted-foreground absolute right-1 left-0 truncate px-1 text-[10px] leading-4"
				style:top={`${(rowTops.get(row.id) ?? 0) + 2}px`}
				title={row.label}
			>
				{row.label}
			</span>
		{/each}
	</div>
	<!-- One focusable group: its keys zoom, and every row it draws is listed for a screen reader as its label. -->
	<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
	<div
		bind:this={host}
		bind:clientWidth={width}
		class="focus-visible:ring-ring relative h-full min-w-0 flex-1 touch-none outline-none select-none focus-visible:ring-2"
		role="group"
		aria-label={label}
		aria-roledescription="time line"
		tabindex="0"
		data-testid="time-strip"
		data-view={`${view.start.toFixed(2)}-${view.end.toFixed(2)}`}
		data-ready={known && renderer !== null && (waveform !== null || wave === 0) ? 'true' : 'false'}
		{onpointerdown}
		{onpointermove}
		{onpointerup}
		onpointercancel={onpointerup}
		{onwheel}
		{onkeydown}
	>
		<canvas bind:this={canvas} class="absolute inset-0 h-full w-full" aria-hidden="true"></canvas>
		{#if failure}
			<p class="text-muted-foreground absolute inset-x-2 top-5 text-xs" role="status">
				{failure}: the time line draws with WebGPU.
			</p>
		{/if}
		<!-- The ruler's times. -->
		<div
			class="pointer-events-none absolute inset-x-0 top-0"
			style:height={`${RULER}px`}
			aria-hidden="true"
		>
			{#each tickLabels as t (t)}
				{@const x = xOf(view, t, width)}
				<!-- Kept inside the strip at its ends, and clear of the zoomed stretch's label at the top right. -->
				{#if x > 14 && x < width - (zoomed ? 96 : 14)}
					<span
						class="text-muted-foreground absolute top-0 -translate-x-1/2 text-[10px] tabular-nums"
						style:left={`${x}px`}>{tickLabel(t, view)}</span
					>
				{/if}
			{/each}
		</div>
		{#if wave > 0 && waveNote}
			<p
				class="text-muted-foreground pointer-events-none absolute inset-x-2 text-xs"
				style:top={`${RULER + wave / 2 - 8}px`}
				data-testid="wave-note"
			>
				{waveNote}
			</p>
		{/if}
		{@render overlay?.({ view, width, top: (row) => rowTops.get(row) ?? 0 })}
		{#if zoomed}
			<span
				class="bg-background/80 text-muted-foreground pointer-events-none absolute top-0 right-1 rounded px-1 text-[10px] tabular-nums"
				data-testid="strip-zoom"
			>
				{formatClock(view.start)}–{formatClock(view.end)}
			</span>
		{/if}
		<!-- What the canvas draws, for a reader that cannot see it: the stretch shown and each row with what it holds. -->
		<div class="sr-only" data-testid="strip-summary">
			<p>{formatClock(view.start)} to {formatClock(view.end)} shown</p>
			<ul>
				{#each rows as row (row.id)}
					<li
						data-row={row.id}
						data-spans={row.spans.length}
						data-markers={row.markers?.length ?? 0}
					>
						{row.label || row.id}: {row.spans.length} stretch{row.spans.length === 1
							? ''
							: 'es'}{row.markers?.length
							? `, ${row.markers.length} keyframe${row.markers.length === 1 ? '' : 's'}`
							: ''}
					</li>
				{/each}
			</ul>
		</div>
	</div>
</div>
