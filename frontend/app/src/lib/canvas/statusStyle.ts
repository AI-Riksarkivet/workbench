import type { BadgeVariant } from '@riksarkivet/ui/badge';

/** Shared status → style map for the review surfaces (list, table, detail). Kept tiny and in
 *  one place so the dot and the badge never disagree, and expressed in the estate's SEMANTIC
 *  tokens (success / warning / destructive / primary), which carry a correct value in both themes. */
export function statusDot(status: string): string {
	switch (status) {
		case 'accepted':
			return 'bg-success';
		case 'rejected':
			return 'bg-destructive';
		case 'prediction':
			return 'bg-warning';
		case 'reviewed':
			return 'bg-primary';
		default:
			return 'bg-muted-foreground';
	}
}

/** The @riksarkivet/ui Badge variant for a status — the same badge vocabulary the data and admin
 *  zones use, so a status chip here reads as the same object it does over there. */
export function statusVariant(status: string): BadgeVariant {
	switch (status) {
		case 'accepted':
			return 'success';
		case 'rejected':
			return 'destructive';
		case 'prediction':
			return 'warning';
		case 'reviewed':
			return 'default';
		default:
			return 'outline';
	}
}

/** What a shape's own status says, in the words the sidebar uses. A shape's status is not its item's review: a shape a
 *  person drew is kept from the moment it is drawn, while the item it is on may still be labeled or in review. */
export function statusWord(status: string): string {
	switch (status) {
		case 'accepted':
			return 'kept';
		case 'prediction':
			return 'proposed';
		case '':
			return '—';
		default:
			return status;
	}
}

/** The sentence a status summary's tooltip gives. */
export const STATUS_MEANING =
	'Each shape’s own status: kept (drawn by a person, or a proposal someone kept), proposed (a model’s, not yet kept or removed), rejected (turned down: it is not published as a label). The item’s review is in the panel above.';
