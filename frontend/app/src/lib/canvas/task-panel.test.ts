import { describe, expect, it } from 'vitest';
import type { TaskDetail } from '#lib/projects/types.ts';
import { panelState } from './task-panel.ts';

const EVENTS: Record<string, [string, string][]> = {
	claimed: [
		['release', 'can_annotate'],
		['save_draft', 'can_annotate'],
		['skip', 'can_annotate'],
		['submit', 'can_annotate'],
	],
	in_review: [
		['accept', 'can_review'],
		['fix_and_accept', 'can_review'],
		['request_changes', 'can_review'],
	],
	changes_requested: [
		['assign', 'can_manage'],
		['claim', 'can_claim'],
	],
};

const task = (state: TaskDetail['state'], over: Partial<TaskDetail> = {}): TaskDetail =>
	({
		task_id: 't1',
		project_id: 'p1',
		state,
		source: { kind: 'chunks', keys: ['d1'] },
		media: { kind: 'image' },
		review_required: true,
		prediction: [],
		review_notes: [],
		legal_events: (EVENTS[state] ?? []).map(([event, permission]) => ({
			event,
			to: '',
			permission,
		})),
		...over,
	}) as TaskDetail;

const ANNOTATOR = ['can_annotate', 'can_claim'];
const REVIEWER = ['can_annotate', 'can_claim', 'can_review'];

describe('the canvas’s side panel', () => {
	it('is the holder’s labeling, offering submit and put back', () => {
		const panel = panelState(task('claimed', { assignee: 'dex~ada' }), ANNOTATOR, 'dex~ada');
		expect([panel.mode, panel.title, panel.actions]).toEqual([
			'annotating',
			'Labeling',
			['release', 'submit'],
		]);
	});

	it('is someone else’s claim, offering nothing', () => {
		const panel = panelState(task('claimed', { assignee: 'dex~bo' }), REVIEWER, 'dex~ada');
		expect([panel.mode, panel.title, panel.actions]).toEqual(['held', 'Held by bo', []]);
	});

	it('is a review of another’s submission by their name, offering accept, fix and accept, and send back', () => {
		const panel = panelState(
			task('in_review', { submitted_by: 'dex~bo', people: { 'dex~bo': 'Bo Ek' } }),
			REVIEWER,
			'dex~ada',
		);
		expect([panel.mode, panel.title, panel.actions]).toEqual([
			'reviewing',
			'Reviewing Bo Ek’s work',
			['accept', 'fix_and_accept', 'request_changes'],
		]);
	});

	it('is a wait for review to the person who submitted it, even one who may review', () => {
		const panel = panelState(task('in_review', { submitted_by: 'dex~ada' }), REVIEWER, 'dex~ada');
		expect([panel.mode, panel.actions]).toEqual(['waiting', []]);
	});

	it('shows the reviewer’s note on an item sent back, and on its next claim', () => {
		const note = {
			by: 'dex~bo',
			at: '2026-11-16T00:00:00Z',
			action: 'request_changes',
			message: 'Box the seal too.',
			shape_ids: [],
		};
		const sent = panelState(
			task('changes_requested', { review_notes: [note] }),
			ANNOTATOR,
			'dex~ada',
		);
		const again = panelState(
			task('claimed', { assignee: 'dex~ada', review_notes: [note] }),
			ANNOTATOR,
			'dex~ada',
		);
		expect([sent.mode, sent.note, sent.actions]).toEqual([
			'changes',
			{ by: 'bo', message: 'Box the seal too.' },
			['claim'],
		]);
		expect([again.mode, again.note?.message]).toEqual(['annotating', 'Box the seal too.']);
	});

	it('is done once accepted, naming who accepted it', () => {
		const panel = panelState(task('accepted', { reviewed_by: 'dex~bo' }), REVIEWER, 'dex~ada');
		expect([panel.mode, panel.detail, panel.actions]).toEqual([
			'accepted',
			'Accepted by bo: the item is done.',
			[],
		]);
	});
});
