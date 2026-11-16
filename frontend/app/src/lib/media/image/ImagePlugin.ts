import { ColorMatrixFilter, Sprite, Texture, type Application } from 'pixi.js';

/** The part of the picture the view shows, in image pixels. */
export interface ViewportBounds {
	x: number;
	y: number;
	width: number;
	height: number;
}

/** Ask the server WHY an image failed, once, after `<img>` has already given up.
 *
 *  `<img>.onerror` exposes neither status nor body, so this re-requests the same URL purely to read
 *  them. It is diagnosis, not a retry: the result is only ever used to build the message. If even
 *  this fails, say the plain thing rather than inventing a cause.
 */
async function explain(url: string): Promise<string> {
	try {
		const res = await fetch(url);
		if (res.ok) {
			// It answers fine on a second look, so the failure was the DECODE — the bytes are not an
			// image the browser understands. Naming the content type is what makes that actionable
			// (an HTML error page and a truncated TIFF both fail here, and they need different fixes).
			return `image could not be decoded (server sent ${res.headers.get('content-type') ?? 'no content-type'}): ${url}`;
		}
		const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
		const detail = typeof body?.detail === 'string' ? body.detail : res.statusText;
		return `${res.status} ${detail} — ${url}`;
	} catch {
		return `Failed to load image: ${url}`;
	}
}

/**
 * A still on a WebGPU Pixi stage: loaded, fitted to the view, zoomed and panned. It knows nothing of what is drawn
 * over it; a layer that draws in image space reads the view through `onViewportChange`.
 */
export class ImagePlugin {
	private app: Application;
	private sprite: Sprite | null = null;
	private dragging = false;
	private lastX = 0;
	private lastY = 0;
	/** Pointer id captured for the active pan drag, or null when not panning */
	private panPointerId: number | null = null;
	/** The touch points down on the canvas, for a two-finger pinch. */
	private touches = new Map<number, { x: number; y: number }>();
	/** The distance between the two touch points at the pinch's last step. */
	private pinchDistance = 0;

	zoom = 1;
	panX = 0;
	panY = 0;
	imageWidth = 0;
	imageHeight = 0;
	/** The loaded still image (null for video-frame backdrops) — the pixel source the
	 *  magnetic tool reads for corner detection. */
	private _imageElement: HTMLImageElement | null = null;
	/** Reused across adjustments so we don't leak a filter per call */
	private colorFilter: ColorMatrixFilter | null = null;

	onViewportChange?: (bounds: ViewportBounds) => void;
	/** Called with the canvas's size each time the picture is fitted to it. */
	onFit?: (width: number, height: number) => void;
	canPan?: () => boolean;

	constructor(app: Application) {
		this.app = app;
		// GPU-accelerated transforms for the viewport container
		app.stage.isRenderGroup = true;
		this.setupEvents();
	}

	/** Counts the loads begun: a load that a later one overtook while its picture was in flight is dropped. */
	private loadSeq = 0;

	async load(url: string): Promise<void> {
		const seq = ++this.loadSeq;
		const img = new Image();
		img.crossOrigin = 'anonymous';
		await new Promise<void>((resolve, reject) => {
			img.onload = () => resolve();
			// `<img>` is kept for the HAPPY path — it streams and decodes natively, and nothing here
			// beats it. But its `onerror` carries NO status and NO body by design, so a server that
			// answered a perfectly clear `404 {"detail": "dataset 'demo' not found"}` would reach the
			// person as "Failed to load image: <url>". So a FAILURE pays one extra request to find out
			// why. The happy path pays nothing.
			img.onerror = () => {
				void explain(url).then((why) => reject(new Error(why)));
			};
			img.src = url;
		});
		if (seq !== this.loadSeq) return;

		if (this.sprite) {
			this.app.stage.removeChild(this.sprite);
			// Destroy the texture/source we are replacing to free GPU memory
			const old = this.sprite.texture;
			this.sprite.destroy();
			old.destroy(true);
		}

		const texture = Texture.from(img);
		this.sprite = new Sprite(texture);
		this.imageWidth = img.naturalWidth;
		this.imageHeight = img.naturalHeight;
		this._imageElement = img;

		// Add as bottom layer on stage
		this.app.stage.addChildAt(this.sprite, 0);
		this.fitToViewport();
	}

	/** The loaded still image, or null when the backdrop is a video frame. */
	get imageElement(): HTMLImageElement | null {
		return this._imageElement;
	}

	/**
	 * Load image from Arrow Binary column data (zero-copy path).
	 * The bytes go from Arrow buffer → Blob → ObjectURL → Image → GPU texture.
	 * No extra HTTP request, no separate endpoint.
	 */
	async loadFromBytes(bytes: Uint8Array, mimeType = 'image/png'): Promise<void> {
		if (this.sprite) {
			this.app.stage.removeChild(this.sprite);
			// Destroy the texture/source we are replacing to free GPU memory
			const old = this.sprite.texture;
			this.sprite.destroy();
			old.destroy(true);
		}

		// Ensure we have a proper ArrayBuffer-backed view for the Blob constructor
		const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
		const blob = new Blob([buffer as ArrayBuffer], { type: mimeType });
		const url = URL.createObjectURL(blob);

		try {
			const img = new Image();
			await new Promise<void>((resolve, reject) => {
				img.onload = () => resolve();
				img.onerror = () => reject(new Error('Failed to load image from Arrow binary data'));
				img.src = url;
			});

			const texture = Texture.from(img);
			this.sprite = new Sprite(texture);
			this.imageWidth = img.naturalWidth;
			this.imageHeight = img.naturalHeight;
			this._imageElement = img;

			this.app.stage.addChildAt(this.sprite, 0);
			this.fitToViewport();
		} finally {
			URL.revokeObjectURL(url);
		}
	}

	/**
	 * Snapshot a video's CURRENT frame as the backdrop, so spatial shapes are drawn on
	 * the exact frame via the SAME PixiJS overlay as images. Uses `createImageBitmap`
	 * (a decoded still) — NOT a Pixi VideoSource, which drives itself off a running
	 * ticker; this engine renders on demand (`app.ticker.stop()`), so a VideoSource would
	 * freeze on a paused frame anyway. Re-callable per seek/frame; keeps the current
	 * zoom/pan across frames (only the first frame fits-to-viewport).
	 */
	async loadFromVideoFrame(video: HTMLVideoElement): Promise<void> {
		if (video.readyState < 2 || video.videoWidth === 0) return; // no frame decoded yet
		const bitmap = await createImageBitmap(video);
		const first = this.sprite === null;
		if (this.sprite) {
			this.app.stage.removeChild(this.sprite);
			const old = this.sprite.texture;
			this.sprite.destroy();
			old.destroy(true); // frees the previous frame's GPU texture + source
		}

		this.sprite = new Sprite(Texture.from(bitmap));
		this.imageWidth = video.videoWidth;
		this.imageHeight = video.videoHeight;
		this._imageElement = null; // a video frame — the CV tools' still-image source doesn't apply
		this.app.stage.addChildAt(this.sprite, 0);
		if (first) this.fitToViewport();
		else this.applyTransform(); // stable view while scrubbing
	}

	/** True once the person zoomed or panned away from the fit: a resize then keeps their view instead of refitting. */
	viewMoved = false;

	/** The margin, in CSS pixels, a fitted picture keeps from each edge of the view. */
	fitPadding = 0;
	/** More margin below, in CSS pixels, where controls float over the bottom of the view. */
	fitBottom = 0;

	fitToViewport(): void {
		if (!this.sprite) return;
		this.viewMoved = false;

		const vw = this.app.screen.width;
		const vh = this.app.screen.height;
		const pad = Math.min(this.fitPadding, vw / 4, vh / 4);
		const below = Math.min(this.fitBottom, vh / 4);
		const width = vw - 2 * pad;
		const height = vh - 2 * pad - below;
		const scale = Math.min(width / this.imageWidth, height / this.imageHeight);

		this.zoom = scale;
		this.panX = pad + (width - this.imageWidth * scale) / 2;
		this.panY = pad + (height - this.imageHeight * scale) / 2;

		this.applyTransform();
		this.onFit?.(vw, vh);
	}

	getViewportBounds(): ViewportBounds {
		const vw = this.app.screen.width;
		const vh = this.app.screen.height;
		return {
			x: -this.panX / this.zoom,
			y: -this.panY / this.zoom,
			width: vw / this.zoom,
			height: vh / this.zoom,
		};
	}

	destroy(): void {
		this.removeEvents();
		if (this.sprite) {
			// Tear down the sprite together with its texture/source to free GPU memory
			this.sprite.destroy({ texture: true, textureSource: true });
			this.sprite = null;
		}
	}

	private applyTransform(): void {
		this.app.stage.scale.set(this.zoom);
		this.app.stage.position.set(this.panX, this.panY);
		this.onViewportChange?.(this.getViewportBounds());
		// Render on demand — no continuous ticker
		this.app.render();
	}

	private get canvas(): HTMLCanvasElement {
		return this.app.canvas as HTMLCanvasElement;
	}

	private setupEvents(): void {
		this.canvas.addEventListener('wheel', this.onWheel, { passive: false });
		this.canvas.addEventListener('pointerdown', this.onPointerDown);
		this.canvas.addEventListener('pointermove', this.onPointerMove);
		this.canvas.addEventListener('pointerup', this.onPointerUp);
		this.canvas.addEventListener('pointercancel', this.onPointerUp);
		// The pinch and the pan are the canvas's own: the browser does not scroll or zoom the page under them.
		this.canvas.style.touchAction = 'none';
		// No pointerleave→up shortcut: pointer capture keeps the pan alive
		// even when the cursor leaves the canvas mid-gesture.
		this.canvas.addEventListener('dblclick', this.onDoubleClick);
	}

	private removeEvents(): void {
		this.canvas.removeEventListener('wheel', this.onWheel);
		this.canvas.removeEventListener('pointerdown', this.onPointerDown);
		this.canvas.removeEventListener('pointermove', this.onPointerMove);
		this.canvas.removeEventListener('pointerup', this.onPointerUp);
		this.canvas.removeEventListener('pointercancel', this.onPointerUp);
		this.canvas.removeEventListener('dblclick', this.onDoubleClick);
	}

	private zoomAt(x: number, y: number, factor: number): void {
		this.viewMoved = true;
		const newZoom = Math.max(0.05, Math.min(50, this.zoom * factor));
		this.panX = x - (x - this.panX) * (newZoom / this.zoom);
		this.panY = y - (y - this.panY) * (newZoom / this.zoom);
		this.zoom = newZoom;
	}

	private onWheel = (e: WheelEvent): void => {
		e.preventDefault();

		const rect = this.canvas.getBoundingClientRect();
		const mx = e.clientX - rect.left;
		const my = e.clientY - rect.top;

		if (e.ctrlKey) {
			// Pinch-to-zoom (trackpad): deltaY is continuous, clamp and scale gently
			const delta = Math.max(-20, Math.min(20, e.deltaY));
			this.zoomAt(mx, my, 1 - delta * 0.003);
		} else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) * 0.5) {
			// Trackpad two-finger pan (deltaX is significant)
			this.viewMoved = true;
			this.panX -= e.deltaX;
			this.panY -= e.deltaY;
		} else {
			// Mouse wheel zoom: use deltaY magnitude for proportional zoom
			// Normalize: most mice send deltaY ~100 per tick, trackpads send ~1-10
			const normalized = Math.max(-150, Math.min(150, e.deltaY));
			const factor = 1 - normalized * 0.001;
			this.zoomAt(mx, my, factor);
		}

		this.applyTransform();
	};

	private onPointerDown = (e: PointerEvent): void => {
		if (this.canPan && !this.canPan()) return;
		if (e.pointerType === 'touch') {
			this.touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
			if (this.touches.size === 2) {
				this.pinchDistance = this.touchSpread();
				return;
			}
		}
		this.dragging = true;
		this.lastX = e.clientX;
		this.lastY = e.clientY;
		this.canvas.style.cursor = 'grabbing';
		// Capture so the pan survives the cursor leaving the canvas mid-drag
		this.panPointerId = e.pointerId;
		this.canvas.setPointerCapture(e.pointerId);
	};

	private onPointerMove = (e: PointerEvent): void => {
		if (this.touches.has(e.pointerId)) {
			this.touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
			if (this.touches.size === 2) {
				const spread = this.touchSpread();
				if (this.pinchDistance > 0 && spread > 0) {
					const rect = this.canvas.getBoundingClientRect();
					const [a, b] = [...this.touches.values()];
					if (a && b) {
						this.zoomAt(
							(a.x + b.x) / 2 - rect.left,
							(a.y + b.y) / 2 - rect.top,
							spread / this.pinchDistance,
						);
						this.applyTransform();
					}
				}
				this.pinchDistance = spread;
				return;
			}
		}
		if (!this.dragging) return;

		this.viewMoved = true;
		this.panX += e.clientX - this.lastX;
		this.panY += e.clientY - this.lastY;
		this.lastX = e.clientX;
		this.lastY = e.clientY;

		this.applyTransform();
	};

	private onPointerUp = (e: PointerEvent): void => {
		this.touches.delete(e.pointerId);
		if (this.touches.size < 2) this.pinchDistance = 0;
		const left = [...this.touches.values()][0];
		if (left && this.touches.size === 1) {
			// The finger still down carries the pan on from where it is, not from where the pinch began.
			this.lastX = left.x;
			this.lastY = left.y;
			return;
		}
		if (this.dragging) {
			this.dragging = false;
			// The pan was the person's to make when it began, so the hand that offers it again is shown.
			this.canvas.style.cursor = 'grab';
		}
		if (this.panPointerId !== null) {
			this.canvas.releasePointerCapture(this.panPointerId);
			this.panPointerId = null;
		}
	};

	/** The distance between the two touch points. */
	private touchSpread(): number {
		const [a, b] = [...this.touches.values()];
		return a && b ? Math.hypot(a.x - b.x, a.y - b.y) : 0;
	}

	private onDoubleClick = (): void => {
		// Double-click intentionally does nothing — use the zoom controls to reset view
	};

	/** Apply image adjustments via ColorMatrixFilter */
	setImageAdjustments(brightness: number, contrast: number, saturation: number): void {
		if (!this.sprite) return;

		// All at defaults (1.0) — remove filter for zero overhead
		// (keep the cached instance around for reuse on the next non-default call)
		if (brightness === 1 && contrast === 1 && saturation === 1) {
			this.sprite.filters = [];
			this.app.render();
			return;
		}

		// Lazily create one filter and reconfigure it in place on every call
		const filter = (this.colorFilter ??= new ColorMatrixFilter());
		filter.reset();
		filter.brightness(brightness, false);
		filter.contrast(contrast - 1, true); // pixi contrast: 0 = normal, -1 = grey, +1 = high
		filter.saturate(saturation - 1, true); // pixi saturate: 0 = normal, -1 = desaturated
		this.sprite.filters = [filter];
		this.app.render();
	}

	/** Whether the picture is larger than the view, so there is somewhere to pan to. */
	get overflows(): boolean {
		return (
			this.imageWidth * this.zoom > this.app.screen.width + 1 ||
			this.imageHeight * this.zoom > this.app.screen.height + 1
		);
	}

	/** Image pixels per CSS pixel: 1 shows the picture at its own size. */
	get scale(): number {
		return this.zoom;
	}

	/** Show the picture at its own size (one image pixel per CSS pixel), centred on the view's middle. */
	actualSize(): void {
		if (!this.sprite || this.zoom <= 0) return;
		const vw = this.app.screen.width;
		const vh = this.app.screen.height;
		this.zoomAt(vw / 2, vh / 2, 1 / this.zoom);
		this.applyTransform();
	}

	/** Programmatic reset — callable from UI regardless of tool */
	resetView(): void {
		this.fitToViewport();
	}

	/** Zoom in by a fixed step, centered on canvas */
	zoomIn(): void {
		const vw = this.app.screen.width;
		const vh = this.app.screen.height;
		this.zoomAt(vw / 2, vh / 2, 1.25);
		this.applyTransform();
	}

	/** Zoom out by a fixed step, centered on canvas */
	zoomOut(): void {
		const vw = this.app.screen.width;
		const vh = this.app.screen.height;
		this.zoomAt(vw / 2, vh / 2, 0.8);
		this.applyTransform();
	}

	/** Move the view by `dx`, `dy` screen pixels: the pan tool's keyboard step. */
	panBy(dx: number, dy: number): void {
		this.viewMoved = true;
		this.panX += dx;
		this.panY += dy;
		this.applyTransform();
	}
}
