/**
 * The tool registry — ONE list driving the toolbar buttons, the shell's hotkey map, the canvas's
 * keyboard gesture and the cv/drawing eligibility flags. Add a tool here (and its engine class in
 * InteractionManager) and every surface picks it up.
 */
import {
	Crosshair,
	Hand,
	Lasso,
	Magnet,
	Minus,
	MousePointer2,
	Paintbrush,
	PenLine,
	Pentagon,
	Square,
} from '@lucide/svelte';
import type { Tool } from './engine/index.ts';

export interface ToolDef {
	tool: Tool;
	icon: typeof MousePointer2;
	label: string;
	/** Hotkey (single char; letters lowercase in the keymap, shown uppercase). Letters, never digits: the digits pick a
	 *  task's classes, as in the labeling tools people know (Label Studio, CVAT). */
	key: string;
	/** A drawing tool (hidden outside edit mode / for non-spatial units). */
	drawing: boolean;
	/** Needs the still-image corner pipeline (unavailable over video frames). */
	cv?: boolean;
	/** Which band of the rail this sits in. Separate from `drawing`, which was doing double duty:
	 *  `lasso` is a DRAWING tool by that flag (it is an edit-mode affordance) while committing no
	 *  shape at all — it selects. Grouping by what a tool is FOR is the distinction a rail needs, and
	 *  it is what makes the order derived rather than hand-arranged. */
	group: ToolGroup;
	/** How the keyboard drives it on the canvas (Space at the arrow-moved cursor): `click` presses and
	 *  releases at once (select, a point, a polygon's vertex), `drag` holds the press until the next Space
	 *  so the arrows draw the gesture (a box, a line, a stroke), and `view` moves the view (pan). */
	gesture: 'click' | 'drag' | 'view';
}

/** Reading order, top to bottom. The rail renders these bands in this sequence. */
export type ToolGroup = 'navigate' | 'draw' | 'assist';
export const TOOL_GROUPS: readonly ToolGroup[] = ['navigate', 'draw', 'assist'];

export const TOOL_DEFS: ToolDef[] = [
	// ── navigate: move around and pick things. Commits nothing, so no task rule ever hides these.
	{
		tool: 'select',
		icon: MousePointer2,
		label: 'Select',
		key: 'V',
		drawing: false,
		gesture: 'click',
		group: 'navigate',
	},
	{
		tool: 'pan',
		icon: Hand,
		label: 'Pan',
		key: 'H',
		drawing: false,
		gesture: 'view',
		group: 'navigate',
	},
	{
		tool: 'lasso',
		icon: Lasso,
		label: 'Lasso',
		key: 'L',
		drawing: true,
		gesture: 'drag',
		group: 'navigate',
	},
	// ── draw: commits a shape. Simple to elaborate, and each is hidden when the task refuses the
	//    shape it makes.
	{
		tool: 'rect',
		icon: Square,
		label: 'Rectangle',
		key: 'B',
		drawing: true,
		gesture: 'drag',
		group: 'draw',
	},
	{
		tool: 'polygon',
		icon: Pentagon,
		label: 'Polygon',
		key: 'G',
		drawing: true,
		gesture: 'click',
		group: 'draw',
	},
	{
		tool: 'point',
		icon: Crosshair,
		label: 'Point',
		key: 'D',
		drawing: true,
		gesture: 'click',
		group: 'draw',
	},
	{
		tool: 'line',
		icon: Minus,
		label: 'Line',
		key: 'N',
		drawing: true,
		gesture: 'drag',
		group: 'draw',
	},
	{
		tool: 'pencil',
		icon: PenLine,
		label: 'Pencil (freehand)',
		key: 'F',
		drawing: true,
		gesture: 'drag',
		group: 'draw',
	},
	{
		tool: 'brush',
		icon: Paintbrush,
		label: 'Brush',
		key: 'U',
		drawing: true,
		gesture: 'drag',
		group: 'draw',
	},
	// ── assist: the machine does part of the drawing. A letter rather than a digit, because it is
	//    categorically different from the nine above and the rail says so.
	{
		tool: 'magnetic',
		icon: Magnet,
		label: 'Magnetic (corner-snap)',
		key: 'M',
		drawing: true,
		cv: true,
		gesture: 'click',
		group: 'assist',
	},
];

/** Split the tools that survived filtering into rail BANDS, in reading order.
 *
 *  Lives here rather than in the component so the test exercises the SAME function the rail renders.
 *  A test that re-implements the grouping can pass while the rail disagrees with it — the mirror is
 *  the bug, not the coverage.
 *
 *  EMPTY BANDS ARE DROPPED: the caller has already hidden the drawing tools the task refuses, so a
 *  bbox-only task empties `assist` entirely, and a separator rendered for an absent band is a
 *  hairline with nothing on either side of it.
 */
export function bandsOf(visible: readonly ToolDef[]): { group: ToolGroup; tools: ToolDef[] }[] {
	return TOOL_GROUPS.map((group) => ({
		group,
		tools: visible.filter((t) => t.group === group),
	})).filter((b) => b.tools.length > 0);
}

/** Hotkey → tool, derived from the registry (lowercased for the keydown handler). */
export const TOOL_KEYS: Record<string, Tool> = Object.fromEntries(
	TOOL_DEFS.map((t) => [t.key.toLowerCase(), t.tool]),
);

export const isCvTool = (tool: Tool): boolean =>
	TOOL_DEFS.some((t) => t.tool === tool && t.cv === true);

/** How the keyboard drives `tool` on the canvas. */
export const gestureOf = (tool: Tool): ToolDef['gesture'] =>
	TOOL_DEFS.find((t) => t.tool === tool)?.gesture ?? 'click';
