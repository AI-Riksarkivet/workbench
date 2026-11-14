import { Workflow } from '@lucide/svelte';
import { seg, type ZoneNav } from '@workbench/ui/shell';

// What remains of the old explorer is its `/workflow` editor alone, until #9 D4 merges it with studio's
// flows in the app. Search, the atlas, the graph and the tree are the app's dataset pages
// (`frontend/app`, `/datasets/[id]/…`).
export function explorerZoneNav(): ZoneNav {
	return {
		title: 'Explorer',
		groups: [],
		footer: {
			label: 'Workspace',
			items: [
				{
					title: 'Workflow',
					href: '/explorer/workflow',
					match: seg('/explorer/workflow'),
					icon: Workflow,
				},
			],
		},
	};
}
