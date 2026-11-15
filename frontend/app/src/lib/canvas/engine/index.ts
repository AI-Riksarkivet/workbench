/**
 * The labeling canvas's engine — public API.
 *
 * Framework-agnostic annotation + rendering engine: PixiJS rendering, the Apache Arrow data layer,
 * interaction/tools/editors and geometry. Imports no Svelte and no
 * `$app`; the canvas's Svelte binding (`#lib/canvas`) is its one consumer.
 *
 * THE PIXI ENTRY. pixi 8 builds its uniform-buffer and shader sync functions with `new Function`, in
 * its WebGPU renderer too (`createUboSyncFunction.mjs`), which the app's CSP refuses: it names no
 * `unsafe-eval`. `pixi.js/unsafe-eval` installs pixi's own precompiled sync functions in their place,
 * so it is imported here, before anything else, and `Application` is re-exported from here: the canvas
 * cannot make a renderer this module has not prepared.
 */
import 'pixi.js/unsafe-eval';

export { Application } from 'pixi.js';

// ── Rendering (PixiJS plugins + context types) ──
export * from './pixi/ImagePlugin.js';
export * from './pixi/ArrowDataPlugin.js';
export * from './pixi/types.js';

// ── Interaction layer ──
export * from './interaction/InteractionManager.js';
export * from './interaction/geometry.js';
export * from './interaction/types.js';

// ── Drawing tools ──
export * from './tools/RectTool.js';
export * from './tools/PolygonTool.js';
export * from './tools/LassoTool.js';
export * from './tools/MagneticTool.js';
export * from './tools/corners.js';
export * from './tools/PencilTool.js';
export * from './tools/PointTool.js';
export * from './tools/LineTool.js';
export * from './tools/BrushTool.js';

// ── Shape editors ──
export * from './editors/RectEditor.js';
export * from './editors/PolygonEditor.js';

// ── Framework-agnostic state stores + Arrow batch construction ──
// The local-first overlay of pending edits is the canvas's controller, not a store here.
export * from './store/batch.js';
export * from './store/LayerStore.js';

// The public `Tool` is the tool-id union from ./pixi/types; the engine-internal `Tool`
// *interface* in ./interaction/types is imported directly by tool classes, not re-exported.
export type { Tool } from './pixi/types.js';
