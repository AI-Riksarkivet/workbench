<script lang="ts">
	// One image unit on the labeling canvas: the shared still viewer (`#lib/media/image`) shows the picture, fits,
	// zooms and pans it; this adds the annotation layer over it (the Arrow shapes and the interaction tools), then
	// the annotations, then the controller attached to both. Remounted per unit by the shell's `{#key}`.
	import { onDestroy } from 'svelte';
	import { loadAnnotations } from '#lib/api/annotations.api.ts';
	import ImageStage, { type ImageStageContext } from '#lib/media/image/ImageStage.svelte';
	import { tableFromArrays } from 'apache-arrow';
	import { ArrowDataPlugin, InteractionManager, type PixiContext } from './engine/index.ts';
	import type { AnnotatorController } from './controller.svelte.ts';

	let {
		imageUrl,
		annotationsUrl,
		controller,
		onload,
		onerror,
	}: {
		imageUrl: string;
		annotationsUrl: string;
		controller: AnnotatorController;
		onload?: (count: number) => void;
		/** A failure's words, and the door's status when the annotations read was refused. */
		onerror?: (message: string, status?: number) => void;
	} = $props();

	// The annotations read is awaited after the stage handed over, and can outlive this component: a stale
	// continuation must not attach the controller after its owner is gone. Checked after the await.
	const EMPTY = tableFromArrays({
		x: new Float32Array(0),
		y: new Float32Array(0),
		width: new Float32Array(0),
		height: new Float32Array(0),
	});
	let alive = true;
	/** The engine this viewer attached the controller to: a viewer mounted again (the page's layout changing) attaches
	 *  its own, and this one, destroyed after, must not take that away. */
	let attached: PixiContext | null = null;
	onDestroy(() => {
		alive = false;
		if (attached !== null && controller.ctx === attached) controller.detach();
	});

	let interaction: InteractionManager | undefined;
	let arrow: ArrowDataPlugin | undefined;
	/** Another replica's shapes while one is compared, under the item's own: drawn only, never hit or edited. */
	let ghost = $state<ArrowDataPlugin | undefined>(undefined);

	/** A compared shape's colour, by how it stands against the item's own. */
	const GHOST: Record<string, number> = {
		theirs: 0xf59e0b,
		changed: 0xdb2777,
		same: 0x94a3b8,
	};

	// The compared version's shapes, each coloured by how it differs: one only it has, one placed or classed otherwise,
	// one alike. The item's own proposals are its own rows, already drawn.
	$effect(() => {
		const layer = ghost;
		const version = controller.compare;
		const diff = controller.compareDiff;
		if (!layer) return;
		const table = version?.table ?? null;
		if (table === null) {
			layer.load(EMPTY);
			layer.sync();
			return;
		}
		layer.clearDeleted();
		layer.load(table);
		layer.sync();
		const kinds = new Map<number, string>();
		// Only the shapes with a place on the picture: a region of time or a span has none here.
		for (const entry of diff) if (entry.theirs?.box) kinds.set(entry.theirs.index, entry.kind);
		for (let i = 0; i < table.numRows; i += 1) {
			const kind = kinds.get(i);
			if (kind === undefined) layer.setDeleted(i);
			else layer.setFieldOverride(i, 'status', kind);
		}
		layer.sync();
	});

	/** Over the picture: the shapes, then the tools; a drag pans only while the pan tool is chosen. */
	function onready({ app, image }: ImageStageContext): () => void {
		const compared = new ArrowDataPlugin(app, (kind) => GHOST[kind] ?? 0x94a3b8, {
			fillAlpha: 0,
			strokeWidth: 2.5,
			strokeAlpha: 0.95,
		});
		compared.setViewport(image.getViewportBounds());
		ghost = compared;
		const shapes = new ArrowDataPlugin(app);
		const im = new InteractionManager(app, shapes);
		arrow = shapes;
		interaction = im;
		shapes.setViewport(image.getViewportBounds());
		const ctx: PixiContext = { app, plugins: { image, arrow: shapes, interaction: im } };
		// The still, for the magnetic tool's corner maps.
		im.setImageSource(image.imageElement);
		void loadLayer(ctx);
		return () => {
			im.destroy();
			shapes.destroy();
			compared.destroy();
			interaction = undefined;
			arrow = undefined;
			ghost = undefined;
		};
	}

	async function loadLayer(ctx: PixiContext): Promise<void> {
		try {
			const loaded = await loadAnnotations(annotationsUrl);
			if (!alive) return;
			if (!loaded.ok) {
				// Left unattached: saving over annotations that could not be read would clobber them.
				onerror?.(loaded.detail, loaded.status);
				return;
			}
			const { table, version } = loaded.data;
			ctx.plugins.arrow.load(table);
			ctx.plugins.arrow.sync();
			controller.attach(ctx, table, annotationsUrl, version);
			attached = ctx;
			onload?.(table.numRows);
		} catch (e) {
			onerror?.(e instanceof Error ? e.message : String(e));
		}
	}
</script>

<ImageStage
	url={imageUrl}
	controls={false}
	canPan={() => interaction?.currentTool === 'pan'}
	{onready}
	onerror={(failure) => onerror?.(failure.message)}
	onviewport={(bounds) => {
		arrow?.setViewport(bounds);
		ghost?.setViewport(bounds);
		interaction?.updateHandles();
	}}
/>
