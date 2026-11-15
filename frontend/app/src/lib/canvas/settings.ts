/** The labeling canvas's settings, as `/config.json` named them (`canvas`), set once at boot before any canvas opens. */
import type { CanvasSettings } from '../config.ts';

let settings: CanvasSettings = { autosaveIdleSeconds: 4 };

export function setCanvasSettings(next: CanvasSettings): void {
	settings = next;
}

export function canvasSettings(): CanvasSettings {
	return settings;
}
