/**
 * What the zoom controls drive: a view of a still that can be zoomed, fitted and shown at its own size. The
 * player's viewer and the labeling canvas each hand theirs to the same controls.
 */
export interface Zoomable {
	/** Image pixels per CSS pixel: 1 is the picture at its own size. */
	readonly scale: number;
	zoomIn(): void;
	zoomOut(): void;
	/** Fit the whole picture to the view. */
	resetView(): void;
	/** Show the picture at its own size. */
	actualSize(): void;
}
