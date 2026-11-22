/**
 * THE TOP-UP on the Label tab (ADR 0034): once per visit while the project's strategy is on, whatever the listing says
 * of it, a round numbers up to 50 items in each lane the strategy orders by, and the list is read again. Then it stops:
 * another round is the person's ("Score more"), offered only while a round can reach an item not yet tried.
 *
 * TWO LANES, each reading the listing in item order with an offset kept for the visit, so an item a round could not
 * number is passed by and the next round reaches the items after it:
 * - by doubt (least sure, mix): the items the project's sampling model has not scored (`scored=false`); the reads door
 *   reads their dataset's score column and names those a model is still to be asked about, which are asked a few at
 *   once (`sampleEach`). Each item still unscored after that is passed by.
 * - by diversity (least alike, mix): the items not measured (`measured=false`); the reads door measures them. Each item
 *   it did not measure is passed by, and a round that measured none offers no more.
 *
 * The model failing stops the round and is said once, and no round starts after it; what a read could not do (no
 * embeddings, nothing labelled yet, no model endpoint) is said in its own words. Stopping it (the tab left) gives up
 * what is in flight and reads nothing again.
 */
import type { ApiResult } from '#lib/api/client.ts';
import { diverse, sampleEach, scores } from './sampling.ts';
import type { SamplingReads, SamplingState, SamplingStrategy, TaskDetail } from './types.ts';

/** The doors a round goes through. */
export interface SamplingDoors {
	/** The project's strategy as the page holds it. */
	strategy(): SamplingStrategy;
	/** Up to 50 of the listed items lacking a score (`scored`) or a novelty (`measured`), in item order from ``offset``. */
	lacking(
		lane: 'scored' | 'measured',
		offset: number,
		signal: AbortSignal,
	): Promise<ApiResult<TaskDetail[]>>;
	/** Read the numbers of these items from their datasets. */
	reads(taskIds: string[], signal: AbortSignal): Promise<ApiResult<SamplingReads>>;
	/** Ask the sampling model about one item. */
	ask(task: TaskDetail, signal: AbortSignal): Promise<ApiResult<unknown>>;
	/** Read the list again. */
	reread(): Promise<unknown>;
}

export class SamplingTopUp {
	/** A round, or a measure, is out. */
	running = $state(false);
	/** What the model said when it could not answer; no round starts once it is set. */
	failure = $state<string | null>(null);
	/** Why a read itself was refused, until the next round. */
	refused = $state<string | null>(null);
	/** What the last read could not do, in its own words. */
	notes = $state.raw<string[]>([]);
	/** The items of each lane a round tried and could not number, passed by from then on. */
	skippedScore = $state(0);
	skippedMeasure = $state(0);
	/** How many items the last round's diversity lane measured; null before any round. */
	lastMeasured = $state<number | null>(null);
	#started = false;
	readonly #stop = new AbortController();
	readonly #doors: SamplingDoors;

	constructor(doors: SamplingDoors) {
		this.#doors = doors;
	}

	/** The visit's first round, once. */
	start(): void {
		if (this.#started) return;
		this.#started = true;
		void this.round();
	}

	/** Whether another round can reach an item not yet tried, by what the listing counts (``state``). */
	canContinue(state: SamplingState): boolean {
		if (this.failure !== null || this.#stop.signal.aborted) return false;
		const byDoubt = scores(state.strategy) && state.unscored - this.skippedScore > 0;
		const byDiversity =
			diverse(state.strategy) &&
			state.unmeasured - this.skippedMeasure > 0 &&
			this.lastMeasured !== 0;
		return byDoubt || byDiversity;
	}

	/** One round over each lane the strategy orders by, then the list read again. Nothing starts while one is out, once
	 *  the model failed, or once it stopped. */
	async round(): Promise<void> {
		const signal = this.#stop.signal;
		if (this.running || this.failure !== null || signal.aborted) return;
		this.#started = true;
		this.running = true;
		this.refused = null;
		const strategy = this.#doors.strategy();
		const notes: string[] = [];
		try {
			if (scores(strategy) && !(await this.#byDoubt(signal, notes))) return;
			if (signal.aborted || this.failure !== null) return;
			if (diverse(strategy) && !(await this.#byDiversity(signal, notes))) return;
			if (signal.aborted) return;
			this.notes = [...new Set(notes)];
			await this.#doors.reread();
		} finally {
			this.running = false;
		}
	}

	/** The doubt lane: read the column, ask the model about the rest, pass by what stays unscored. False when a read
	 *  was refused or the round was stopped. */
	async #byDoubt(signal: AbortSignal, notes: string[]): Promise<boolean> {
		const listed = await this.#doors.lacking('scored', this.skippedScore, signal);
		if (signal.aborted) return false;
		if (!listed.ok) return this.#refuse(listed.detail);
		if (listed.data.length === 0) return true;
		const read = await this.#doors.reads(
			listed.data.map((t) => t.task_id),
			signal,
		);
		if (signal.aborted) return false;
		if (!read.ok) return this.#refuse(read.detail);
		notes.push(...read.data.notes);
		const fromColumn = new Set(read.data.scored.map((s) => s.task_id));
		const asked = new Set(read.data.ask);
		const { scored, failure } = await sampleEach(
			listed.data.filter((t) => asked.has(t.task_id)),
			this.#doors.ask,
			signal,
		);
		if (signal.aborted) return false;
		if (failure !== null) {
			this.failure = failure;
			this.notes = [...new Set(notes)];
			await this.#doors.reread();
			return false;
		}
		this.skippedScore += listed.data.filter(
			(t) => !fromColumn.has(t.task_id) && !scored.has(t.task_id),
		).length;
		return true;
	}

	/** The diversity lane: measure, and pass by what was not measured. */
	async #byDiversity(signal: AbortSignal, notes: string[]): Promise<boolean> {
		const listed = await this.#doors.lacking('measured', this.skippedMeasure, signal);
		if (signal.aborted) return false;
		if (!listed.ok) return this.#refuse(listed.detail);
		if (listed.data.length === 0) {
			this.lastMeasured = 0;
			return true;
		}
		const read = await this.#doors.reads(
			listed.data.map((t) => t.task_id),
			signal,
		);
		if (signal.aborted) return false;
		if (!read.ok) return this.#refuse(read.detail);
		notes.push(...read.data.notes);
		const measured = new Set(read.data.measured.map((n) => n.task_id));
		this.skippedMeasure += listed.data.filter((t) => !measured.has(t.task_id)).length;
		this.lastMeasured = measured.size;
		return true;
	}

	#refuse(detail: string): false {
		this.refused = detail;
		return false;
	}

	/** Measure again how unlike the labelled items each of ``taskIds`` is, and read the list again. */
	async measure(taskIds: string[]): Promise<void> {
		const signal = this.#stop.signal;
		if (this.running || signal.aborted || taskIds.length === 0) return;
		this.running = true;
		this.refused = null;
		try {
			const read = await this.#doors.reads(taskIds, signal);
			if (signal.aborted) return;
			if (!read.ok) {
				this.refused = read.detail;
				return;
			}
			this.notes = read.data.notes;
			if (read.data.measured.length > 0) this.lastMeasured = read.data.measured.length;
			await this.#doors.reread();
		} finally {
			this.running = false;
		}
	}

	/** Give up what is in flight and start nothing more. */
	stop(): void {
		this.#stop.abort();
	}
}
