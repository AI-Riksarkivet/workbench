/**
 * The canvas-row → task-draft-shape mapping (S10 first half).
 *
 * Every fixture uses the literal the ENGINE commits, not a value it never produces: a fixture that invents its input
 * cannot fail the way production does, and a rename on either side of the seam is a named failure here rather than
 * shapes silently missing from a publish.
 */
import { tableFromArrays } from 'apache-arrow';
import { describe, expect, it } from 'vitest';

import { rowsToShapes } from './draft-shapes.ts';

describe('rowsToShapes', () => {
	it('maps the annotation columns onto draft shapes', () => {
		const table = tableFromArrays({
			id: ['a1', 'a2'],
			// what the canvas actually writes: RectTool commits 'rect', historically stored as
			// 'rectangle'. Both must normalize to the canonical 'bbox'.
			shape_type: ['rectangle', 'polygon'],
			x: Float32Array.from([10, 0]),
			y: Float32Array.from([12, 0]),
			width: Float32Array.from([80, 0]),
			height: Float32Array.from([40, 0]),
			label: ['portrait', 'stamp'],
			difficult: [false, true],
		});

		const shapes = rowsToShapes(table);

		expect(shapes).toHaveLength(2);
		expect(shapes[0]).toMatchObject({
			shape_id: 'a1',
			shape_type: 'bbox',
			x: 10,
			y: 12,
			width: 80,
			height: 40,
			label: 'portrait',
			difficult: false,
		});
		expect(shapes[1]).toMatchObject({ shape_id: 'a2', shape_type: 'polygon', difficult: true });
	});

	it('skips rows without a shape_type rather than fabricating geometry', () => {
		const table = tableFromArrays({
			id: ['t1'],
			shape_type: [''],
			label: ['tag-only'],
		});
		expect(rowsToShapes(table)).toHaveLength(0);
	});
});

describe('the engine/service vocabulary seam', () => {
	it('normalizes every drawing tool the engine can commit', () => {
		// The exact strings the tools emit (RectTool, PointTool, LineTool, PolygonTool, BrushTool), each of
		// which must reach the service under a name its draft model accepts.
		const table = tableFromArrays({
			id: ['r', 'p', 'l', 'g', 'm', 'legacy'],
			shape_type: ['rect', 'point', 'line', 'polygon', 'mask', 'rectangle'],
		});

		expect(rowsToShapes(table).map((s) => s.shape_type)).toEqual([
			'bbox',
			'keypoint',
			'polyline',
			'polygon',
			'mask',
			'bbox',
		]);
	});

	it('drops a shape type neither side knows rather than passing it through', () => {
		// An unknown type reaching the service is refused at submit with a message about TOOLS,
		// which reads as a template problem and sends the reader to entirely the wrong place.
		const table = tableFromArrays({ id: ['x'], shape_type: ['spline'] });
		expect(rowsToShapes(table)).toHaveLength(0);
	});
});
