<script lang="ts">
	// The Pixi surface: one Application, its three plugins, sized to its container and rendered on demand.
	//
	// WebGPU ONLY: `preference: ['webgpu']`. pixi 8 appends its other renderers to a STRING preference
	// (`autoDetectRenderer`), so `'webgpu'` would fall back to WebGL; an array admits only what it lists, and a browser
	// without WebGPU is a failed init, said through `onerror` in the words the atlas uses (`#lib/gpu`). The renderer
	// pixi made is named on `data-renderer`.
	//
	// `data-fitted` is the size, in CSS pixels, the picture was last fitted to. While it differs from the box the canvas
	// is laid out in, a resize is still on its way to the view, and a point read off the box is not yet the picture's.
	import { onMount } from 'svelte';
	import {
		Application,
		ArrowDataPlugin,
		ImagePlugin,
		InteractionManager,
		type PixiContext,
	} from './engine/index.ts';
	import { gpuUnsupportedReason } from '#lib/gpu/gpu-support.ts';

	let {
		onready,
		onerror,
	}: { onready?: (ctx: PixiContext) => void; onerror?: (message: string) => void } = $props();

	let containerEl: HTMLDivElement;
	let renderer = $state<string | null>(null);
	let fitted = $state<string | null>(null);

	onMount(() => {
		const app = new Application();
		let imagePlugin: ImagePlugin | undefined;
		let arrowPlugin: ArrowDataPlugin | undefined;
		let interaction: InteractionManager | undefined;
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
			// The canvas's name and focus are its container's (CanvasShell), which the keyboard drives.
			canvas.setAttribute('aria-hidden', 'true');
			// eslint-disable-next-line svelte/no-dom-manipulating -- Pixi owns this canvas; the container renders no Svelte children
			containerEl.appendChild(canvas);
			initialized = true;
			renderer = app.renderer.name;

			// Render on demand only.
			app.ticker.stop();

			// A resize refits the picture while the person has not yet moved the view themselves: the filmstrip, the
			// inspector and the page's own layout settle after the first fit, and a picture fitted to the first size
			// would sit off centre. Once they pan or zoom, a resize keeps their view.
			resizeObs = new ResizeObserver((entries) => {
				if (destroyed || !initialized) return;
				const entry = entries[0];
				if (!entry) return;
				const { width: w, height: h } = entry.contentRect;
				if (w > 0 && h > 0) {
					app.renderer.resize(w, h);
					if (imagePlugin && !imagePlugin.viewMoved) imagePlugin.resetView();
					app.render();
				}
			});
			resizeObs.observe(containerEl);

			const image = new ImagePlugin(app);
			const arrow = new ArrowDataPlugin(app);
			const im = new InteractionManager(app, arrow);
			imagePlugin = image;
			arrowPlugin = arrow;
			interaction = im;
			image.canPan = () => im.currentTool === 'pan';
			image.onFit = (width, height) => (fitted = `${width}x${height}`);
			image.onViewportChange = (bounds) => {
				arrow.setViewport(bounds);
				im.updateHandles();
			};
			onready?.({ app, plugins: { image, arrow, interaction: im } });
		}

		start().catch(async (err: unknown) => {
			if (destroyed) return;
			onerror?.((await gpuUnsupportedReason()) ?? `the canvas could not start: ${String(err)}`);
		});

		return () => {
			destroyed = true;
			resizeObs?.disconnect();
			interaction?.destroy();
			arrowPlugin?.destroy();
			imagePlugin?.destroy();
			if (initialized) app.destroy(true, { children: true });
		};
	});
</script>

<div
	bind:this={containerEl}
	class="relative h-full w-full overflow-hidden"
	data-testid="pixi-canvas"
	data-renderer={renderer}
	data-fitted={fitted}
></div>
