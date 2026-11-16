/**
 * The dataset dock's layout: which panels exist, the arrangement a first visit opens with, and the
 * edits the dock's own controls make to a `LayoutConfig` (close a panel, open one, read a stored one).
 *
 * Pure functions over plain objects, so the arrangement a person saves is the arrangement they get
 * back: `horizon-layout` takes the whole layout as ONE bindable value, and everything here returns a
 * new value rather than reaching into the library's state.
 */
import {
	parseLayoutConfig,
	validateConfig,
	type Id,
	type LayoutConfig,
	type NodeConfig,
	type SplitConfig,
	type TabGroupConfig,
} from 'horizon-layout';
import type { DockPanel, PanelGates } from '#lib/explore/pane-capabilities.ts';

/** Every panel the workbench can show, in the order the panel picker lists them. */
export const PANELS = [
	{ id: 'results', title: 'Results', keywords: ['search', 'results', 'hits', 'thumbnails'] },
	{
		id: 'player',
		title: 'Player',
		keywords: ['player', 'media', 'image', 'video', 'audio', 'transcript', 'speakers'],
	},
	{ id: 'atlas', title: 'Atlas', keywords: ['atlas', 'map', 'projection', 'embedding'] },
	{ id: 'graph', title: 'Graph', keywords: ['graph', 'knowledge', 'entities', 'cypher'] },
	{ id: 'tree', title: 'Tree', keywords: ['tree', 'topics', 'treemap', 'hierarchy'] },
] as const satisfies readonly { id: DockPanel; title: string; keywords: readonly string[] }[];

export type PanelId = (typeof PANELS)[number]['id'];

/** The panels' names on a dataset shown as a plain table: the results and the player hold the table's
 *  rows and the row opened. */
export const PLAIN_TITLES: Readonly<Record<string, string>> = { results: 'Rows', player: 'Row' };

const PANEL_IDS: ReadonlySet<string> = new Set(PANELS.map((p) => p.id));

export function isPanelId(id: string): id is PanelId {
	return PANEL_IDS.has(id);
}

/** The views beside the results and the player, in the order a default layout tabs them. */
const VIEW_PANELS: readonly PanelId[] = ['atlas', 'graph', 'tree'];

/**
 * What a default layout is fitted to: the dataset's shape (`image` for still pages, `time` for audio and
 * video, `plain` for a table with no descriptor) and which view panels it can feed (`gates`, from
 * `pane-capabilities`).
 */
export interface LayoutFit {
	kind: 'image' | 'time' | 'plain';
	gates: PanelGates;
}

/** Every panel's gate open: the fit a layout is checked against when no dataset is in view. */
const EVERY_VIEW: LayoutFit = {
	kind: 'image',
	gates: { results: null, player: null, atlas: null, graph: null, tree: null },
};

const tabGroup = (tabs: Id[]): TabGroupConfig => ({
	tabs: tabs as TabGroupConfig['tabs'],
	activeTabIndex: 0,
});

/**
 * The arrangement a dataset opens with, before the person arranges it: only the panels the dataset can
 * feed, the views it declares tabbed together (the atlas in front) so none hides behind a menu.
 *
 * - A page corpus (`image`) sets the player in a full-height middle column, since a portrait page fitted
 *   into a short pane is drawn too small to read: the results on the left, the views on the right.
 * - A recording (`time`) has a short player (the transport and the transcript), so the results take the
 *   left half and the player sits above the views on the right.
 * - A plain table opens its rows and the row opened, nothing else.
 *
 * On a narrow screen (`narrow`) the results take most of the height, full width, and every other panel
 * shares the rest as tabs of one pane, the player in front: a pick opens it there. Split points stay inside
 * horizon-layout's minimums (a tenth of the width, a fifth of the height), or it renders no dock.
 */
export function defaultLayout(narrow = false, fit: LayoutFit = EVERY_VIEW): LayoutConfig {
	const views = VIEW_PANELS.filter((id) => fit.gates[id] === null);
	if (fit.kind === 'plain') {
		return {
			root: {
				direction: narrow ? 'vertical' : 'horizontal',
				views: [tabGroup(['results']), tabGroup(['player'])],
				splitPoints: [0.6],
			},
		};
	}
	if (narrow) {
		return {
			root: {
				direction: 'vertical',
				views: [tabGroup(['results']), tabGroup(['player', ...views])],
				splitPoints: [0.55],
			},
		};
	}
	if (views.length === 0) {
		return {
			root: {
				direction: 'horizontal',
				views: [tabGroup(['results']), tabGroup(['player'])],
				splitPoints: [fit.kind === 'image' ? 0.42 : 0.5],
			},
		};
	}
	if (fit.kind === 'image') {
		return {
			root: {
				direction: 'horizontal',
				views: [tabGroup(['results']), tabGroup(['player']), tabGroup(views)],
				splitPoints: [0.32, 0.7],
			},
		};
	}
	return {
		root: {
			direction: 'horizontal',
			views: [
				tabGroup(['results']),
				{
					direction: 'vertical',
					views: [tabGroup(['player']), tabGroup(views)],
					splitPoints: [0.45],
				},
			],
			splitPoints: [0.48],
		},
	};
}

const isSplit = (node: NodeConfig): node is SplitConfig => 'splitPoints' in node;

function tabsOf(node: NodeConfig | undefined): Id[] {
	if (node === undefined) return [];
	return isSplit(node) ? node.views.flatMap((v) => tabsOf(v)) : [...node.tabs];
}

/** Every panel on screen: docked in a group, popped out, or maximized. */
function openPanels(config: LayoutConfig): Set<Id> {
	return new Set([...tabsOf(config.root), ...(config.popouts ?? [])]);
}

/** The panels the picker offers: every known panel not on screen. */
export function closedPanels(config: LayoutConfig): PanelId[] {
	const open = openPanels(config);
	return PANELS.map((p) => p.id).filter((id) => !open.has(id));
}

/** `node` without `id`, or undefined when nothing is left of it; a split left with one child is
 *  replaced by that child, as the library does when a drag empties a group. */
function withoutTab(node: NodeConfig, id: Id): NodeConfig | undefined {
	if (!isSplit(node)) {
		const index = node.tabs.indexOf(id);
		if (index === -1) return node;
		const tabs = node.tabs.filter((t) => t !== id);
		if (tabs.length === 0) return undefined;
		const active = node.activeTabIndex > index ? node.activeTabIndex - 1 : node.activeTabIndex;
		return {
			tabs: tabs as TabGroupConfig['tabs'],
			activeTabIndex: Math.min(active, tabs.length - 1),
		};
	}
	const kept: { node: NodeConfig; size: number }[] = [];
	const bounds = [0, ...node.splitPoints, 1];
	node.views.forEach((child, i) => {
		const next = withoutTab(child, id);
		if (next !== undefined) kept.push({ node: next, size: bounds[i + 1]! - bounds[i]! });
	});
	if (kept.length === 0) return undefined;
	if (kept.length === 1) return kept[0]!.node;
	// The removed pane's share is spread over the rest in proportion, so the survivors keep their
	// relative sizes.
	const total = kept.reduce((sum, k) => sum + k.size, 0);
	const points: number[] = [];
	let edge = 0;
	for (const k of kept.slice(0, -1)) {
		edge += k.size / total;
		points.push(Number(edge.toFixed(4)));
	}
	return {
		direction: node.direction,
		views: kept.map((k) => k.node) as SplitConfig['views'],
		splitPoints: points as SplitConfig['splitPoints'],
	};
}

/** Close a panel wherever it is: docked, popped out or maximized. */
export function closePanel(config: LayoutConfig, id: Id): LayoutConfig {
	const root = config.root === undefined ? undefined : withoutTab(config.root, id);
	const popouts = (config.popouts ?? []).filter((p) => p !== id);
	return {
		...(root ? { root } : {}),
		...(config.maximizedView && config.maximizedView !== id
			? { maximizedView: config.maximizedView }
			: {}),
		...(popouts.length > 0 ? { popouts } : {}),
	};
}

/** The tab group holding `id`, or the first group when none does. */
function groupOf(node: NodeConfig, id: Id | undefined): TabGroupConfig | undefined {
	if (!isSplit(node)) return id === undefined || node.tabs.includes(id) ? node : undefined;
	for (const child of node.views) {
		const found = groupOf(child, id);
		if (found) return found;
	}
	return undefined;
}

function mapGroups(node: NodeConfig, fn: (group: TabGroupConfig) => TabGroupConfig): NodeConfig {
	if (!isSplit(node)) return fn(node);
	return { ...node, views: node.views.map((v) => mapGroups(v, fn)) as SplitConfig['views'] };
}

/**
 * Open a closed panel as the active tab of the group that holds `beside` (the group whose control
 * was used), or of the first group; an empty dock gets a group of its own.
 */
export function openPanel(config: LayoutConfig, id: Id, beside?: Id): LayoutConfig {
	if (openPanels(config).has(id)) return config;
	const { root } = config;
	if (root === undefined) return { ...config, root: { tabs: [id], activeTabIndex: 0 } };
	const target = groupOf(root, beside) ?? groupOf(root, undefined);
	return {
		...config,
		root: mapGroups(root, (group) =>
			group === target
				? { tabs: [...group.tabs, id] as TabGroupConfig['tabs'], activeTabIndex: group.tabs.length }
				: group,
		),
	};
}

/** The panels a person sees now: the front tab of every group, every popped-out panel, or the maximized
 *  one alone. */
export function shownPanels(config: LayoutConfig): Id[] {
	if (config.maximizedView) return [config.maximizedView];
	const fronts = (node: NodeConfig | undefined): Id[] =>
		node === undefined
			? []
			: isSplit(node)
				? node.views.flatMap((v) => fronts(v))
				: node.tabs[node.activeTabIndex] !== undefined
					? [node.tabs[node.activeTabIndex]!]
					: [];
	return [...fronts(config.root), ...(config.popouts ?? [])];
}

/**
 * Bring `id` in front of the person: a maximized other panel is restored, a tab behind another is
 * brought to the front of its group, and a closed panel is opened. One already shown is left as it is.
 */
export function revealPanel(config: LayoutConfig, id: Id): LayoutConfig {
	if (shownPanels(config).includes(id)) return config;
	let next: LayoutConfig = config;
	if (next.maximizedView !== undefined) {
		next = { ...next };
		delete next.maximizedView;
		if (shownPanels(next).includes(id)) return next;
	}
	const { root } = next;
	if (root === undefined || groupOf(root, id) === undefined) return openPanel(next, id);
	return {
		...next,
		root: mapGroups(root, (g) =>
			g.tabs.includes(id) ? { ...g, activeTabIndex: g.tabs.indexOf(id) } : g,
		),
	};
}

/** Maximize `id`, opening it first where it is closed. */
export function maximizePanel(config: LayoutConfig, id: Id): LayoutConfig {
	const opened = openPanels(config).has(id) ? config : openPanel(config, id);
	return { ...opened, maximizedView: id };
}

/** Pop a docked panel out into a window of its own. */
export function popOut(config: LayoutConfig, id: Id): LayoutConfig {
	const docked = closePanel(config, id);
	return { ...docked, popouts: [...(docked.popouts ?? []), id] };
}

/** The three outcomes of reading a stored layout. `unreadable` means one exists and could not be
 *  read: it is reported and never replaced by the default, so no save overwrites it. */
export type LayoutRead =
	| { readonly status: 'ok'; readonly layout: LayoutConfig }
	| { readonly status: 'absent' }
	| { readonly status: 'unreadable'; readonly detail: string };

const KNOWN: ReadonlyMap<Id, true> = new Map(PANELS.map((p) => [p.id, true] as const));

/** A stored value as a layout this workbench can show: parsed, then validated against its panels. */
export function readStoredLayout(raw: unknown): LayoutRead {
	try {
		const layout = parseLayoutConfig(raw);
		validateConfig(layout, KNOWN);
		// A popout's window closes with the page that opened it, so a stored popout is docked again:
		// left in `popouts`, it would count as open with no window to show it in.
		let docked: LayoutConfig = { ...layout };
		delete docked.popouts;
		for (const id of layout.popouts ?? []) docked = openPanel(docked, id);
		return { status: 'ok', layout: docked };
	} catch (e) {
		return {
			status: 'unreadable',
			detail: `the stored layout is not one this workbench can show: ${e instanceof Error ? e.message : String(e)}`,
		};
	}
}
