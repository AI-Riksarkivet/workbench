/**
 * THE TOP-UP on a reviewer's list (ADR 0026): the listed items no judge has read are asked about, each once while the
 * list is open, and the list read again when any was judged, which may bring more unread items onto the page. The judge
 * failing stops it for good, said once (`judgeEach`); an item refused for itself stays unread and is not asked again.
 * Stopping it (the list closed) gives up the asks in flight, and reads nothing again.
 */
import { SvelteSet } from 'svelte/reactivity';
import type { ApiResult } from '#lib/api/client.ts';
import { judgeEach } from './judge.ts';
import type { TaskDetail } from './types.ts';

export class JudgeTopUp {
	/** A round is out. */
	judging = $state(false);
	/** What the judge said when it could not answer; no round starts once it is set. */
	failure = $state<string | null>(null);
	/** The items asked about while the list is open. */
	readonly #asked = new SvelteSet<string>();
	readonly #stop = new AbortController();
	readonly #judge: (task: TaskDetail, signal: AbortSignal) => Promise<ApiResult<unknown>>;
	readonly #reread: () => Promise<unknown>;

	constructor(
		judge: (task: TaskDetail, signal: AbortSignal) => Promise<ApiResult<unknown>>,
		reread: () => Promise<unknown>,
	) {
		this.#judge = judge;
		this.#reread = reread;
	}

	/** Ask about the unread items of ``tasks`` not asked before, unless a round is out, the judge failed or it stopped. */
	async offer(tasks: readonly TaskDetail[]): Promise<void> {
		if (this.judging || this.failure !== null || this.#stop.signal.aborted) return;
		const unread = tasks.filter((t) => t.judgement === null && !this.#asked.has(t.task_id));
		if (unread.length === 0) return;
		for (const t of unread) this.#asked.add(t.task_id);
		this.judging = true;
		const { judged, failure } = await judgeEach(unread, this.#judge, this.#stop.signal);
		this.judging = false;
		if (this.#stop.signal.aborted) return;
		this.failure = failure;
		if (judged > 0) await this.#reread();
	}

	/** Give up the asks in flight and start no more. */
	stop(): void {
		this.#stop.abort();
	}
}
