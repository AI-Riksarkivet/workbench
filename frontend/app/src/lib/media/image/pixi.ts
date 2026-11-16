/**
 * The one way into pixi for every WebGPU stage in the app.
 *
 * pixi 8 builds its uniform-buffer and shader sync functions with `new Function`, in its WebGPU renderer too
 * (`createUboSyncFunction.mjs`), which the app's CSP refuses: it names no `unsafe-eval`. `pixi.js/unsafe-eval`
 * installs pixi's own precompiled sync functions in their place, so it is imported here, before anything else, and
 * `Application` is re-exported from here: no stage can make a renderer this module has not prepared.
 */
import 'pixi.js/unsafe-eval';

export { Application } from 'pixi.js';
