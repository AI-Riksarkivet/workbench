/**
 * A model's answer as the rows it is saved as: each proposal a `prediction` row from `model:<name>`, naming the model's
 * version and the proposal's score, which the annotator's save refuses a model's row without (`schema.NewAnnotation`).
 * The canvas (`controller.assist`) and the Label grid (`label-run.askEach`) save an answer this one way.
 */
import { canonicalShapeType } from '@workbench/labeling/shape-types';
import type { InsertRow } from '@workbench/labeling/annotations-client';
import type { AssistAnswer, AssistProposal } from '#lib/api/assist.api.ts';

/** The `source` a model's rows are saved under. */
export const modelSource = (model: string): string => `model:${model}`;

/** The ids of the proposals among ``rows`` that ``source`` made and nobody kept or removed yet: what a new answer of
 *  the same model replaces. */
export function proposalsFrom(
	rows: Iterable<{ id: unknown; status: unknown; source: unknown }>,
	source: string,
): string[] {
	const ids: string[] = [];
	for (const row of rows)
		if (row.status === 'prediction' && row.source === source) ids.push(String(row.id));
	return ids;
}

/** The fields of the row ``proposal`` of ``answer`` is saved as, for `makeInsertRow` (which mints its id). */
export function proposalFields(
	answer: Pick<AssistAnswer, 'model' | 'version'>,
	proposal: AssistProposal,
): Partial<InsertRow> & { shape_type: string } {
	return {
		shape_type: canonicalShapeType(proposal.shape_type) ?? proposal.shape_type,
		x: proposal.x,
		y: proposal.y,
		width: proposal.width,
		height: proposal.height,
		polygon: proposal.polygon,
		label: proposal.label,
		text: proposal.text,
		status: 'prediction',
		source: modelSource(answer.model),
		model_version: answer.version,
		confidence: proposal.score,
	};
}
