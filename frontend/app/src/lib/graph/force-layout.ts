/**
 * A graph's node positions for the WebGPU renderer (`./gpu-graph.svelte`): a d3-force layout settled before
 * it is drawn, so the nodes mount in their final places and the camera fits once, not three times while a
 * live simulation moves. Shared by the stamped knowledge graph (`./graph-explorer.svelte.ts`) and a plain
 * table's set-up graph (`#lib/dock/panels/PlainGraphPanel.svelte`).
 */
import {
	forceCollide,
	forceLink,
	forceManyBody,
	forceSimulation,
	forceX,
	forceY,
	type SimulationNodeDatum,
} from 'd3-force';

type SimNode = SimulationNodeDatum & { id: string; idx: number };

/** A node's size on the canvas, by how much it is mentioned (clips, or a plain graph's degree). */
const sizeOf = (mentions: number): number => 8 + Math.min(20, Math.sqrt(Math.max(1, mentions)) * 4);

export interface LaidOut {
	idToIdx: Map<string, number>;
	x: Float32Array<ArrayBuffer>;
	y: Float32Array<ArrayBuffer>;
	size: Float32Array<ArrayBuffer>;
	/** Index pairs of the edges whose two ends are both laid out. */
	edges: Uint32Array<ArrayBuffer>;
}

/** Lay `nodes` out with `edges` pulling them together; the simulation settles synchronously (milliseconds
 *  at the few thousand nodes either graph answers). */
export function layoutGraph(
	nodes: readonly { id: string; mentions: number }[],
	edges: readonly { source: string; target: string }[],
): LaidOut {
	const n = nodes.length;
	const idx = new Map(nodes.map((nd, i) => [nd.id, i]));
	const x = new Float32Array(n);
	const y = new Float32Array(n);
	const size = new Float32Array(n);
	const R = 40 + n * 1.5;
	for (let i = 0; i < n; i++) {
		const a = (i / Math.max(1, n)) * Math.PI * 2;
		x[i] = Math.cos(a) * R;
		y[i] = Math.sin(a) * R;
		size[i] = sizeOf(nodes[i]!.mentions);
	}
	const valid = edges.filter((e) => idx.has(e.source) && idx.has(e.target));
	const ei = new Uint32Array(valid.length * 2);
	valid.forEach((e, i) => {
		ei[i * 2] = idx.get(e.source)!;
		ei[i * 2 + 1] = idx.get(e.target)!;
	});
	const simNodes: SimNode[] = nodes.map((nd, i) => ({ id: nd.id, idx: i, x: x[i], y: y[i] }));
	if (n > 0) {
		const links = valid.map((e) => ({ source: e.source, target: e.target }));
		const sim = forceSimulation<SimNode>(simNodes)
			.stop()
			.force('charge', forceManyBody<SimNode>().strength(-220))
			.force(
				'link',
				forceLink<SimNode, { source: string; target: string }>(links)
					.id((d) => d.id)
					.distance(46)
					.strength(0.35),
			)
			.force(
				'collide',
				forceCollide<SimNode>((d) => sizeOf(nodes[d.idx]?.mentions ?? 1) / 2 + 5),
			)
			.force('x', forceX<SimNode>(0).strength(0.04))
			.force('y', forceY<SimNode>(0).strength(0.04));
		for (let tick = 0; sim.alpha() > 0.025 && tick < 400; tick++) sim.tick();
		sim.stop();
		for (let i = 0; i < n; i++) {
			x[i] = simNodes[i]!.x ?? 0;
			y[i] = simNodes[i]!.y ?? 0;
		}
	}
	return { idToIdx: idx, x, y, size, edges: ei };
}

/** The padded extent of laid-out positions, which the renderer's camera fits. */
export function fitOf(
	xs: Float32Array,
	ys: Float32Array,
): { minX: number; minY: number; maxX: number; maxY: number } {
	const n = xs.length;
	if (n === 0) return { minX: -1, minY: -1, maxX: 1, maxY: 1 };
	let minX = Infinity;
	let minY = Infinity;
	let maxX = -Infinity;
	let maxY = -Infinity;
	for (let i = 0; i < n; i++) {
		minX = Math.min(minX, xs[i]!);
		minY = Math.min(minY, ys[i]!);
		maxX = Math.max(maxX, xs[i]!);
		maxY = Math.max(maxY, ys[i]!);
	}
	const padX = (maxX - minX) * 0.08 || 1;
	const padY = (maxY - minY) * 0.08 || 1;
	return { minX: minX - padX, minY: minY - padY, maxX: maxX + padX, maxY: maxY + padY };
}
