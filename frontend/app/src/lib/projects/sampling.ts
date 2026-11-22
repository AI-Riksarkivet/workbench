/**
 * Sampling on a project's lists (ADR 0034): what each item's reason says, what the line over a list says of the
 * project's sampling, and the asks that score the items a list holds that no model has scored yet. A strategy decides
 * nothing: it orders the queue the Items and Label tabs serve, and a person may still label in any order.
 *
 * WHAT STOPS AN ASK. A model that cannot answer (the endpoint failed, is unavailable or timed out: 502, 503, 504) or a
 * project whose sampling was turned off meanwhile (409 `no-sampling`) fails every item alike, so the first such answer
 * stops the round and is said. Any other refusal is about one item: it stays unscored, and the rest are still asked.
 */
import type { ApiResult } from '#lib/api/client.ts';
import { pooled, type Control } from './label-run.ts';
import type { Chosen, ItemSampling, SamplingState, SamplingStrategy, TaskDetail } from './types.ts';

/** Each strategy as a person reads it. */
export const STRATEGY_LABELS: Record<SamplingStrategy, string> = {
	off: 'Off',
	least_sure: 'Least sure',
	least_alike: 'Least alike',
	mix: 'Mix',
};

/** The strategies that order by a model's doubt, and those that order by how unlike the labelled items an item is. */
const SCORED: readonly SamplingStrategy[] = ['least_sure', 'mix'];
const DIVERSE: readonly SamplingStrategy[] = ['least_alike', 'mix'];
export const scores = (strategy: SamplingStrategy): boolean => SCORED.includes(strategy);
export const diverse = (strategy: SamplingStrategy): boolean => DIVERSE.includes(strategy);

const number = (n: number): string => n.toFixed(2);

/** Why one item stands where it does: the model's doubt (0..1), or its distance from the nearest labelled item. */
export function reasonText(kind: 'least_sure' | 'least_alike', score: number): string {
	return kind === 'least_sure'
		? `Least sure · doubt ${number(score)}`
		: `Least alike · ${number(score)}`;
}

/** The reason an item of a list shows while ``strategy`` is on: what placed it there, or else what sampling knows of
 *  it; null while sampling is off. A quiet one says it has no number yet. */
export function samplingChip(
	item: ItemSampling | null | undefined,
	strategy: SamplingStrategy,
): { text: string; quiet: boolean } | null {
	if (strategy === 'off') return null;
	const doubt = item?.score?.kind === 'least_sure' ? item.score.score : null;
	const novelty = item?.novelty?.novelty ?? null;
	if (item?.placed_by === 'least_sure' && doubt !== null)
		return { text: reasonText('least_sure', doubt), quiet: false };
	if (item?.placed_by === 'least_alike' && novelty !== null)
		return { text: reasonText('least_alike', novelty), quiet: false };
	if (item?.score?.kind === 'proposed_nothing')
		return { text: 'Model proposed nothing', quiet: false };
	if (SCORED.includes(strategy) && doubt !== null)
		return { text: reasonText('least_sure', doubt), quiet: false };
	if (DIVERSE.includes(strategy) && novelty !== null)
		return { text: reasonText('least_alike', novelty), quiet: false };
	return { text: 'Not scored yet', quiet: true };
}

/** What sampling knows of an item, in full, for its reason's tooltip: who scored or measured it, with what, and when. */
export function samplingDetail(
	item: ItemSampling | null | undefined,
	when: (iso: string) => string = (iso) => new Date(iso).toLocaleString(),
): string[] {
	const lines: string[] = [];
	const score = item?.score;
	if (score)
		lines.push(
			`${score.kind === 'proposed_nothing' ? 'Proposed nothing the item takes' : `Doubt ${number(score.score ?? 0)}`}: ${score.model} ${score.version}, asked by ${score.asked_by}, ${when(score.scored_at)}`,
		);
	const novelty = item?.novelty;
	if (novelty)
		lines.push(
			`Distance ${number(novelty.novelty)} from the nearest of ${novelty.against} labelled item${novelty.against === 1 ? '' : 's'}, by ${novelty.vectors}${novelty.model ? ` (${novelty.model})` : ''}, measured for ${novelty.asked_by}, ${when(novelty.measured_at)}`,
		);
	return lines;
}

/** Why an item was sent, where its send says: sampling chose it (by which strategy, with which number), or a person
 *  picked it beside a proposal. */
export function chosenLine(chosen: Chosen | null | undefined): string | null {
	if (!chosen) return null;
	if (chosen.by === 'person') return 'Picked by a person';
	if (chosen.kind === null) return 'Chosen by sampling';
	const kind = chosen.kind === 'least_sure' ? 'least sure' : 'least alike';
	return chosen.score === null
		? `Chosen by sampling: ${kind}`
		: `Chosen by sampling: ${kind} (${number(chosen.score)})`;
}

/** The line over a list of the project's sampling: how many of its items have the numbers its strategy orders by, or
 *  why it cannot order the list; null while sampling is off. */
export function samplingLine(state: SamplingState): { warn: boolean; text: string } | null {
	if (state.strategy === 'off') return null;
	if (!state.available)
		return { warn: true, text: `Sampling cannot order this list: ${state.reason}` };
	const label = STRATEGY_LABELS[state.strategy];
	const scored = `${state.scored} scored, ${state.unscored} not yet`;
	const measured = `${state.measured} measured, ${state.unmeasured} not yet`;
	const counts =
		state.strategy === 'least_sure'
			? scored
			: state.strategy === 'least_alike'
				? measured
				: `${scored}; ${measured}`;
	const by = state.model && SCORED.includes(state.strategy) ? ` by ${state.model}` : '';
	return { warn: false, text: `${label}${by}: ${counts}.` };
}

/** What is said of the items no model can be asked about (``asks`` false) and no score column scored. */
export function unaskedLine(state: SamplingState): string | null {
	if (!scores(state.strategy) || state.asks || state.unscored === 0) return null;
	return `No model can be asked about the ${state.unscored} item${state.unscored === 1 ? '' : 's'} their dataset does not score.`;
}

/** Whether a refused ask says the model itself cannot answer, rather than something about the one item asked. */
function modelCannotAnswer(refused: { status: number; type?: string }): boolean {
	return (
		[502, 503, 504].includes(refused.status) ||
		(refused.status === 409 && refused.type === 'no-sampling')
	);
}

/** A control that runs until ``signal`` aborts or ``stop`` says so, telling no progress. */
const until = (signal: AbortSignal, stop: () => boolean): Control => ({
	progress: () => {},
	stopped: () => signal.aborted || stop(),
	signal,
});

/** Ask the sampling model about each of ``tasks``, a few at once, until ``signal`` aborts or the model cannot answer:
 *  the ids it scored, and what stopped it. An item refused for itself is passed over and stays unscored. */
export async function sampleEach(
	tasks: readonly TaskDetail[],
	ask: (task: TaskDetail, signal: AbortSignal) => Promise<ApiResult<unknown>>,
	signal: AbortSignal,
): Promise<{ scored: Set<string>; failure: string | null }> {
	let failure: string | null = null;
	const answers = await pooled(
		tasks,
		async (task) => {
			const answer = await ask(task, signal);
			if (!answer.ok && modelCannotAnswer(answer)) failure ??= answer.detail;
			return answer.ok;
		},
		until(signal, () => failure !== null),
		false,
	);
	return { scored: new Set(tasks.filter((_, i) => answers[i]).map((t) => t.task_id)), failure };
}
