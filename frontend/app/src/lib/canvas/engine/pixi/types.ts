import type { Application } from 'pixi.js';
import type { ArrowDataPlugin } from './ArrowDataPlugin.js';
import type { ImagePlugin } from '#lib/media/image/ImagePlugin.ts';
import type { InteractionManager } from '../interaction/InteractionManager.js';

export interface PixiContext {
	app: Application;
	plugins: {
		image: ImagePlugin;
		arrow: ArrowDataPlugin;
		interaction: InteractionManager;
	};
}

export type { ViewportBounds } from '#lib/media/image/ImagePlugin.ts';

export type Tool =
	| 'select'
	| 'pan'
	| 'rect'
	| 'polygon'
	| 'magnetic'
	| 'lasso'
	| 'pencil'
	| 'point'
	| 'line'
	| 'brush';
