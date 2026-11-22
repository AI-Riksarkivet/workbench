/**
 * The judge on a reviewer's list (ADR 0026): what its line says of the project's judge, the asks that judge the items a
 * page lists that no judge has read yet, and the queue that asks it about each item a person submits. A verdict decides
 * nothing; it orders and marks the list.
 *
 * WHAT STOPS AN ASK. A judge that cannot answer (the endpoint failed, is unavailable or timed out: 502, 503, 504) or a
 * project that names none (409 `no-judge`) fails every item alike, so the first such answer stops the round and is said.
 * Any other refusal is about one item (it left review, the model refused its picture, it has none, the caller may not
 * judge it): that item stays unread, and the rest are still asked.
 */
import type { ApiResult } from '#lib/api/client.ts';
import { IN_FLIGHT, pooled, type Control } from './label-run.ts';
import type { JudgeState, TaskDetail } from './types.ts';

/** The judge's line over a reviewer's list: how it has read the items waiting, why it cannot be asked, or (quietly)
 *  that the project names none. */
export function judgeLine(judge: JudgeState): { tone: keyof typeof TONE_CLASS; text: string } {
	if (judge.model === '') return { tone: 'quiet', text: 'This project has no judge.' };
	if (!judge.available) return { tone: 'warn', text: `The judge is unavailable: ${judge.reason}` };
	const parts = [`${judge.flagged} flagged`, `${judge.clear} clear`];
	if (judge.unread > 0) parts.push(`${judge.unread} not judged`);
	return { tone: 'told', text: `Judge ${judge.model}: ${parts.join(', ')}.` };
}

/** How each tone of the judge's line is set. */
export const TONE_CLASS = {
	quiet: 'text-muted-foreground text-xs',
	told: 'text-sm',
	warn: 'text-warning text-sm',
} as const;

/** Whether a refused ask says the judge itself cannot answer, rather than something about the one item asked. */
function judgeCannotAnswer(refused: { status: number; type?: string }): boolean {
	return (
		[502, 503, 504].includes(refused.status) ||
		(refused.status === 409 && refused.type === 'no-judge')
	);
}

/** What an item not asked answers in a round: nothing judged. */
const NOT_ASKED = false;

/** A control that runs until ``signal`` aborts or ``stop`` says so, telling no progress. */
const until = (signal: AbortSignal, stop: () => boolean = () => false): Control => ({
	progress: () => {},
	stopped: () => signal.aborted || stop(),
	signal,
});

/** Ask the judge about each of ``tasks``, a few at once, until ``signal`` aborts or the judge cannot answer: how many it
 *  judged, and what stopped it. An item refused for itself is passed over and stays unread. */
export async function judgeEach(
	tasks: readonly TaskDetail[],
	judge: (task: TaskDetail, signal: AbortSignal) => Promise<ApiResult<unknown>>,
	signal: AbortSignal,
): Promise<{ judged: number; failure: string | null }> {
	let failure: string | null = null;
	const answers = await pooled(
		tasks,
		async (task) => {
			const answer = await judge(task, signal);
			if (!answer.ok && judgeCannotAnswer(answer)) failure ??= answer.detail;
			return answer.ok;
		},
		until(signal, () => failure !== null),
		NOT_ASKED,
	);
	return { judged: answers.filter(Boolean).length, failure };
}

/**
 * THE ASKS AFTER A SUBMIT: one queue for the page, so a bulk submit of many items asks the judge at most `IN_FLIGHT` at
 * a time rather than all at once. While it holds asks it reads once per project whether that project names a judge, and
 * asks only for items of one that does. Nothing here is waited for or said: an ask that fails leaves its item unread,
 * which the Review tab asks about.
 */
export class SubmitAsks {
	readonly #waiting: TaskDetail[] = [];
	#running = 0;
	/** Whether each project names a judge, read once while the queue holds asks. */
	#named = new Map<string, Promise<boolean>>();
	readonly #names: (projectId: string) => Promise<boolean>;
	readonly #judge: (task: TaskDetail) => Promise<unknown>;

	constructor(
		names: (projectId: string) => Promise<boolean>,
		judge: (task: TaskDetail) => Promise<unknown>,
	) {
		this.#names = names;
		this.#judge = judge;
	}

	/** Ask about ``task`` once the asks before it have gone out, if it landed in review. */
	add(task: TaskDetail): void {
		if (task.state !== 'in_review') return;
		this.#waiting.push(task);
		this.#next();
	}

	#next(): void {
		while (this.#running < IN_FLIGHT) {
			const task = this.#waiting.shift();
			if (task === undefined) return;
			this.#running += 1;
			void this.#ask(task).finally(() => {
				this.#running -= 1;
				if (this.#running === 0 && this.#waiting.length === 0) this.#named = new Map();
				this.#next();
			});
		}
	}

	async #ask(task: TaskDetail): Promise<void> {
		let named = this.#named.get(task.project_id);
		if (named === undefined) {
			named = this.#names(task.project_id).catch(() => false);
			this.#named.set(task.project_id, named);
		}
		if (await named) await this.#judge(task).catch(() => undefined);
	}
}
