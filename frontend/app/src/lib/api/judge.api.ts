/**
 * The judge door (ADR 0026): `POST /api/judge/<unit key>` asks the model a project names what it disagrees with among
 * the labels of one submitted item, and keeps the verdict; an item already judged answers the verdict kept and asks no
 * model. Addressed as the annotations and assist doors are: the unit's key in the path, its project, task and dataset
 * in the query. The annotator asks the judge in the caller's own request (the item's submitter, or someone who may
 * review it), so the app asks it when a submit lands and when a reviewer's list holds items no judge has read.
 *
 * A verdict decides nothing: the reviewer still accepts or sends back every item. The calls here are not a person's
 * writes, so none is recorded in their activity log. Every submit site calls `askJudgeAfterSubmit` itself, so firing
 * a transition never asks a judge unseen.
 */
import { writeJSON, type ApiResult } from './client.ts';
import { serviceUrl } from './services.ts';
import { JudgementSchema, type Judgement, type TaskDetail } from '#lib/projects/types.ts';
import { SubmitAsks } from '#lib/projects/judge.ts';
import { fetchProject } from './projects.api.ts';

/** The item a judge reads: its task, its project and the unit it names. */
type Judged = Pick<TaskDetail, 'task_id' | 'project_id' | 'source'>;

/** The verdict on ``task``'s submission of now: the one kept, or the judge's answer, asked now and kept; ``signal``
 *  gives the ask up. */
export function requestJudgement(
	task: Judged,
	signal?: AbortSignal,
): Promise<ApiResult<Judgement>> {
	const key = task.source.keys[0] ?? '';
	const path = key.split('/').map(encodeURIComponent).join('/');
	const query = new URLSearchParams({ project: task.project_id, task: task.task_id });
	if (task.source.where) query.set('dataset', task.source.where);
	return writeJSON(
		serviceUrl('annotator', `/api/judge/${path}?${query}`),
		null,
		JudgementSchema,
		'the annotator',
		signal ? { signal } : {},
	);
}

/** The page's one queue of asks after a submit (`SubmitAsks`): each submit that lands in review asks the project's judge
 *  about the item, a few at a time, when the project names one. */
const submitAsks = new SubmitAsks(
	async (projectId) => {
		const read = await fetchProject({ projectId });
		return read.ok && read.data.project.judge_model !== '';
	},
	(task) => requestJudgement(task),
);

/** Ask the project's judge about ``task`` in the background after its submit, if it landed in review. The submit never
 *  waits for it, and its failure is not said: the Review tab asks about every item still unread. */
export function askJudgeAfterSubmit(task: TaskDetail): void {
	submitAsks.add(task);
}
