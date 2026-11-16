<script lang="ts" module>
	import type { Application } from './pixi.ts';
	import type { ImagePlugin, ViewportBounds } from './ImagePlugin.ts';

	/** The stage a layer draws over: the Pixi application and the picture's plugin, the picture loaded and fitted. */
	export interface ImageStageContext {
		app: Application;
		image: ImagePlugin;
	}

	/** Why the stage shows no picture: no WebGPU here, or the picture did not load. */
	export type ImageStageFailure = { kind: 'gpu' | 'load'; message: string };

	export type { ViewportBounds };
</script>

<script lang="ts">
	// The shared still viewer: one WebGPU Pixi stage that shows a picture, fits it to the box it is laid out in, and
	// zooms (wheel, trackpad pinch, two-finger touch, the controls, the keyboard) and pans (drag, the keyboard). It is
	// read-only and draws nothing over the picture; a layer that does (the labeling canvas) is handed the stage in
	// `onready` and reads the view through `onviewport`.
	//
	// WebGPU ONLY: `preference: ['webgpu']`. pixi 8 appends its other renderers to a STRING preference
	// (`autoDetectRenderer`), so `'webgpu'` would fall back to WebGL; an array admits only what it lists, and a browser
	// without WebGPU is a failed init, said through `onerror` in the words the atlas uses (`#lib/gpu`). The renderer
	// pixi made is named on `data-renderer`.
	//
	// `data-fitted` is the size, in CSS pixels, the picture was last fitted to. While it differs from the box the
	// stage is laid out in, a resize is still on its way to the view, and a point read off the box is not yet the
	// picture's. `aria-busy` is on while a picture is loading.
	import { onMount, type Snippet } from 'svelte';
	import { gpuUnsupportedReason } from '#lib/gpu/gpu-support.ts';
	import { Application as PixiApplication } from './pixi.ts';
	import { ImagePlugin as Plugin } from './ImagePlugin.ts';
	import ZoomControls from './ZoomControls.svelte';
	import type { Zoomable } from './zoom.ts';

	let {
		url,
		label,
		canPan,
		keyboard = false,
		controls = true,
		controlsClass,
		fitPadding = 0,
		onready,
		onerror,
		onviewport,
		children,
	}: {
		/** The picture's address: a signed link, which serves without a token. */
		url: string;
		/** The picture's accessible name; without one the stage is decoration of a region that names itself. */
		label?: string;
		/** Whether a drag pans now; always, unless a layer over the stage takes the pointer for its own gestures. */
		canPan?: () => boolean;
		/** The zoom and pan keys on the focused stage: `+` `-` `0` `1`, and the arrows while the picture overflows. */
		keyboard?: boolean;
		/** Whether the stage draws its own zoom controls. */
		controls?: boolean;
		controlsClass?: string;
		/** The margin, in CSS pixels, a fitted picture keeps from the stage's edges. */
		fitPadding?: number;
		/** Once the first picture is loaded and fitted. What it returns runs before the stage is torn down. */
		onready?: (stage: ImageStageContext) => (() => void) | void;
		onerror?: (failure: ImageStageFailure) => void;
		onviewport?: (bounds: ViewportBounds) => void;
		/** Drawn over the stage, inside its box. */
		children?: Snippet;
	} = $props();

	let containerEl: HTMLDivElement;
	let hostEl: HTMLDivElement;
	let renderer = $state<string | null>(null);
	let fitted = $state<string | null>(null);
	let plugin = $state.raw<Plugin | null>(null);
	let scale = $state(1);
	let loading = $state(true);

	const zoom: Zoomable = {
		get scale() {
			return scale;
		},
		zoomIn: () => plugin?.zoomIn(),
		zoomOut: () => plugin?.zoomOut(),
		resetView: () => plugin?.resetView(),
		actualSize: () => plugin?.actualSize(),
	};

	/** The picture asked for last: a load that finishes after another was asked for is not reported. */
	let wanted = '';

	async function show(image: Plugin, next: string): Promise<boolean> {
		wanted = next;
		loading = true;
		try {
			await image.load(next);
			if (wanted !== next) return false;
			loading = false;
			return true;
		} catch (err) {
			if (wanted !== next) return false;
			loading = false;
			onerror?.({ kind: 'load', message: err instanceof Error ? err.message : String(err) });
			return false;
		}
	}

	onMount(() => {
		const app = new PixiApplication();
		let image: Plugin | undefined;
		let layerCleanup: (() => void) | void = undefined;
		let resizeObs: ResizeObserver | null = null;
		let initialized = false;
		let destroyed = false;

		async function start(): Promise<void> {
			// Sized by a ResizeObserver rather than `resizeTo`, so a resize re-renders with the ticker stopped.
			const { width, height } = containerEl.getBoundingClientRect();
			await app.init({
				width: Math.max(width, 1),
				height: Math.max(height, 1),
				preference: ['webgpu'],
				backgroundAlpha: 0,
				background: 'transparent',
				antialias: true,
				resolution: window.devicePixelRatio || 1,
				autoDensity: true,
			});
			if (destroyed) {
				app.destroy(true, { children: true });
				return;
			}
			const canvas = app.canvas as HTMLCanvasElement;
			canvas.style.display = 'block';
			canvas.style.maxWidth = '100%';
			canvas.style.maxHeight = '100%';
			canvas.style.cursor = 'grab';
			// The picture's name is its host's, and the keys are the stage's.
			canvas.setAttribute('aria-hidden', 'true');
			// eslint-disable-next-line svelte/no-dom-manipulating -- Pixi owns this canvas; the host renders no Svelte children
			hostEl.appendChild(canvas);
			initialized = true;
			renderer = app.renderer.name;

			// Render on demand only.
			app.ticker.stop();

			// A resize refits the picture while the person has not yet moved the view themselves: the panels around the
			// stage settle after the first fit, and a picture fitted to the first size would sit off centre. Once they
			// pan or zoom, a resize keeps their view.
			resizeObs = new ResizeObserver((entries) => {
				if (destroyed || !initialized) return;
				const entry = entries[0];
				if (!entry) return;
				const { width: w, height: h } = entry.contentRect;
				if (w > 0 && h > 0) {
					app.renderer.resize(w, h);
					if (image && !image.viewMoved) image.resetView();
					app.render();
				}
			});
			resizeObs.observe(containerEl);

			image = new Plugin(app);
			image.fitPadding = fitPadding;
			// The zoom controls float over the bottom of the stage: a fitted picture is kept clear of them.
			if (controls) image.fitBottom = 40;
			if (canPan) image.canPan = canPan;
			image.onFit = (w, h) => (fitted = `${w}x${h}`);
			const loaded = image;
			image.onViewportChange = (bounds) => {
				scale = loaded.scale;
				onviewport?.(bounds);
			};
			plugin = image;
			if (!(await show(image, url)) || destroyed) return;
			layerCleanup = onready?.({ app, image });
		}

		start().catch(async (err: unknown) => {
			if (destroyed) return;
			const reason = await gpuUnsupportedReason();
			onerror?.(
				reason !== null
					? { kind: 'gpu', message: reason }
					: { kind: 'gpu', message: `the viewer could not start: ${String(err)}` },
			);
		});

		return () => {
			destroyed = true;
			resizeObs?.disconnect();
			layerCleanup?.();
			image?.destroy();
			plugin = null;
			if (initialized) app.destroy(true, { children: true });
		};
	});

	// A new picture in the same stage (the next page of a document): the GPU device and the view's box are kept.
	let shownUrl = '';
	$effect(() => {
		const next = url;
		const image = plugin;
		if (image === null) return;
		if (shownUrl === '') {
			shownUrl = next;
			return;
		}
		if (next === shownUrl) return;
		shownUrl = next;
		void show(image, next);
	});

	const PAN_STEP = 48;
	const ARROWS: Record<string, [number, number]> = {
		ArrowLeft: [1, 0],
		ArrowRight: [-1, 0],
		ArrowUp: [0, 1],
		ArrowDown: [0, -1],
	};

	function onkeydown(e: KeyboardEvent): void {
		const image = plugin;
		if (!keyboard || image === null || e.ctrlKey || e.metaKey || e.altKey) return;
		const arrow = ARROWS[e.key];
		if (arrow) {
			// At the fit there is nowhere to pan, and the arrows are left to what holds the stage (the next page).
			if (!image.overflows) return;
			const step = e.shiftKey ? PAN_STEP * 4 : PAN_STEP;
			image.panBy(arrow[0] * step, arrow[1] * step);
		} else if (e.key === '+' || e.key === '=') image.zoomIn();
		else if (e.key === '-' || e.key === '_') image.zoomOut();
		else if (e.key === '0') image.resetView();
		else if (e.key === '1') image.actualSize();
		else return;
		e.preventDefault();
		e.stopPropagation();
	}
</script>

<!-- The stage: a group that takes its own keys when the holder asks for them, holding the picture (named as an image,
     its canvas hidden) and the zoom controls beside it, which an `img` role would hide from assistive technology. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
	bind:this={containerEl}
	class="focus-visible:ring-ring relative h-full w-full overflow-hidden outline-none focus-visible:ring-2 focus-visible:ring-inset"
	role={keyboard ? 'group' : undefined}
	aria-label={keyboard ? label : undefined}
	aria-roledescription={keyboard ? 'image viewer' : undefined}
	aria-busy={loading}
	tabindex={keyboard ? 0 : undefined}
	data-testid="pixi-canvas"
	data-renderer={renderer}
	data-fitted={fitted}
	{onkeydown}
>
	<div
		bind:this={hostEl}
		class="absolute inset-0"
		role={label ? 'img' : undefined}
		aria-label={label}
	></div>
	{@render children?.()}
	{#if controls && plugin !== null}
		<ZoomControls target={zoom} class={controlsClass} />
	{/if}
</div>
