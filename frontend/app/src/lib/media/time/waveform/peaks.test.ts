import { describe, expect, it } from 'vitest';
import { columnsFor, decodePeaks, halve, waveformOf } from './peaks.ts';

/** Signed bytes, base64: what the viewer answers. */
const b64 = (bytes: number[]): string =>
	btoa(String.fromCharCode(...bytes.map((b) => (b + 256) % 256)));

describe('a waveform', () => {
	it('decodes the answer’s signed bytes to [-1, 1]', () => {
		expect([...decodePeaks(b64([-127, 127, 0, 64]))]).toEqual([-1, 1, 0, Math.fround(64 / 127)]);
	});

	it('halves a level by the min of two mins and the max of two maxes, keeping an odd last bucket', () => {
		const level = { rate: 4, values: new Float32Array([-0.5, 0.25, -0.25, 1, -1, 0.5]) };
		expect(halve(level)).toEqual({ rate: 2, values: new Float32Array([-0.5, 1, -1, 0.5]) });
	});

	it('is built down to a level of at most the floor', () => {
		const answer = {
			duration: 8,
			decoded: 8,
			rate: 1,
			audio: true,
			peaks: b64(Array(16).fill(10)),
			cut: null,
		};
		expect(waveformOf(answer, 2).levels.map((l) => l.values.length / 2)).toEqual([8, 4, 2]);
	});

	it('draws each column from the coarsest level still finer than a column, nothing past what the decode covered', () => {
		const answer = {
			duration: 4,
			decoded: 2,
			rate: 2,
			audio: true,
			peaks: b64([-10, 10, -20, 20, -30, 30, -40, 40]),
			cut: 'time' as const,
		};
		const columns = columnsFor(waveformOf(answer, 1), 0, 4, 4);
		expect([...columns].map((v) => (Number.isNaN(v) ? 'gap' : Math.round(v * 127)))).toEqual([
			-20,
			20,
			-40,
			40,
			'gap',
			'gap',
			'gap',
			'gap',
		]);
	});
});
