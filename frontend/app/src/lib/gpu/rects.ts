/**
 * Coloured rectangles on a WebGPU canvas, drawn as one instanced quad each: what a recording's time axis is made of (the
 * waveform's columns, the lanes, the segments and regions, a track's keyframes, the playhead). Hand-written rather than a
 * plotting library because the waveform must draw with WebGPU (#10) and needs nothing a quad does not give; the bring-up is
 * the canvases' shared one (`webgpu.ts`).
 *
 * Rectangles are in CSS pixels from the canvas's top left, with straight (not premultiplied) colours; the canvas is
 * sized in device pixels by the caller's `resize`. Drawn on demand: nothing animates unless the caller draws again.
 */
import { acquireGpuCanvas } from './webgpu.ts';

/** Floats per rectangle: x0, y0, x1, y1, then r, g, b, a in [0, 1]. */
export const RECT_FLOATS = 8;

const SHADER = /* wgsl */ `
struct View { size: vec2f };
@group(0) @binding(0) var<uniform> view: View;

struct Out {
	@builtin(position) position: vec4f,
	@location(0) colour: vec4f,
};

@vertex
fn vs(@builtin(vertex_index) corner: u32, @location(0) rect: vec4f, @location(1) colour: vec4f) -> Out {
	let xs = array<f32, 6>(0.0, 1.0, 0.0, 0.0, 1.0, 1.0);
	let ys = array<f32, 6>(0.0, 0.0, 1.0, 1.0, 0.0, 1.0);
	let p = vec2f(mix(rect.x, rect.z, xs[corner]), mix(rect.y, rect.w, ys[corner]));
	var out: Out;
	out.position = vec4f(p.x / view.size.x * 2.0 - 1.0, 1.0 - p.y / view.size.y * 2.0, 0.0, 1.0);
	out.colour = colour;
	return out;
}

@fragment
fn fs(in: Out) -> @location(0) vec4f {
	return vec4f(in.colour.rgb * in.colour.a, in.colour.a);
}
`;

export class RectRenderer {
	private capacity = 0;
	private instances: GPUBuffer | null = null;
	private destroyed = false;

	private constructor(
		private readonly device: GPUDevice,
		private readonly context: GPUCanvasContext,
		private readonly pipeline: GPURenderPipeline,
		private readonly uniforms: GPUBuffer,
		private readonly bindGroup: GPUBindGroup,
	) {}

	/** A renderer on `canvas`, or the reason there is none (no WebGPU here). */
	static async create(canvas: HTMLCanvasElement): Promise<RectRenderer | { reason: string }> {
		const gpu = await acquireGpuCanvas(canvas);
		if (!gpu.ok) return { reason: gpu.reason };
		const { device, ctx, format } = gpu;
		const module = device.createShaderModule({ code: SHADER });
		const pipeline = device.createRenderPipeline({
			layout: 'auto',
			vertex: {
				module,
				entryPoint: 'vs',
				buffers: [
					{
						arrayStride: RECT_FLOATS * 4,
						stepMode: 'instance',
						attributes: [
							{ shaderLocation: 0, offset: 0, format: 'float32x4' },
							{ shaderLocation: 1, offset: 16, format: 'float32x4' },
						],
					},
				],
			},
			fragment: {
				module,
				entryPoint: 'fs',
				targets: [
					{
						format,
						blend: {
							color: { srcFactor: 'one', dstFactor: 'one-minus-src-alpha' },
							alpha: { srcFactor: 'one', dstFactor: 'one-minus-src-alpha' },
						},
					},
				],
			},
			primitive: { topology: 'triangle-list' },
		});
		const uniforms = device.createBuffer({
			size: 16,
			usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
		});
		const bindGroup = device.createBindGroup({
			layout: pipeline.getBindGroupLayout(0),
			entries: [{ binding: 0, resource: { buffer: uniforms } }],
		});
		return new RectRenderer(device, ctx, pipeline, uniforms, bindGroup);
	}

	/** Draw `rects` (`RECT_FLOATS` floats each, `count` of them) over a canvas `width` x `height` CSS pixels, cleared to
	 *  transparent first. */
	draw(rects: Float32Array, count: number, width: number, height: number): void {
		if (this.destroyed || width <= 0 || height <= 0) return;
		const { device } = this;
		if (count > this.capacity) {
			this.instances?.destroy();
			this.capacity = Math.max(1024, 2 ** Math.ceil(Math.log2(count)));
			this.instances = device.createBuffer({
				size: this.capacity * RECT_FLOATS * 4,
				usage: GPUBufferUsage.VERTEX | GPUBufferUsage.COPY_DST,
			});
		}
		device.queue.writeBuffer(this.uniforms, 0, new Float32Array([width, height, 0, 0]));
		if (count > 0 && this.instances)
			device.queue.writeBuffer(
				this.instances,
				0,
				rects.buffer,
				rects.byteOffset,
				count * RECT_FLOATS * 4,
			);
		const encoder = device.createCommandEncoder();
		const pass = encoder.beginRenderPass({
			colorAttachments: [
				{
					view: this.context.getCurrentTexture().createView(),
					clearValue: { r: 0, g: 0, b: 0, a: 0 },
					loadOp: 'clear',
					storeOp: 'store',
				},
			],
		});
		if (count > 0 && this.instances) {
			pass.setPipeline(this.pipeline);
			pass.setBindGroup(0, this.bindGroup);
			pass.setVertexBuffer(0, this.instances);
			pass.draw(6, count);
		}
		pass.end();
		device.queue.submit([encoder.finish()]);
	}

	/** Release the device: a time axis that unmounts gives its GPU back. */
	destroy(): void {
		if (this.destroyed) return;
		this.destroyed = true;
		this.instances?.destroy();
		this.uniforms.destroy();
		this.device.destroy();
	}
}

/** A CSS colour as straight RGBA floats, through the browser's own parser (an OKLCH token converts as the page paints
 *  it); `alpha` multiplies the colour's own. */
export function rgba(css: string, alpha = 1): [number, number, number, number] {
	const context = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
	if (!context) return [0, 0, 0, alpha];
	context.clearRect(0, 0, 1, 1);
	context.fillStyle = '#000000';
	context.fillStyle = css;
	context.fillRect(0, 0, 1, 1);
	const [r = 0, g = 0, b = 0, a = 255] = context.getImageData(0, 0, 1, 1).data;
	return [r / 255, g / 255, b / 255, (a / 255) * alpha];
}
