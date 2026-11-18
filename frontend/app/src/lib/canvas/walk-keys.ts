/**
 * Whether a key pressed now belongs to what has the focus, not to the canvas's item-level keys (a reviewer's verdict,
 * Shift+A/F/R, and the walk's Page Up/Down): a form control (a field, a select, a contenteditable), anything inside a
 * menu, a listbox, a combobox or a dialog, or any open dialog or menu on the page. Those keys are the control's own (a
 * select's Page Down moves its choice), and an item-level key fired under an open dialog would act behind it.
 */

/** The fields of the focused element this reads. Structural, so it is answered without a browser. */
export interface FocusTarget {
	tagName: string;
	isContentEditable?: boolean;
	closest(selector: string): unknown;
}

const OWN_KEYS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);
/** What owns its keys, wherever it is: a menu, a listbox, a combobox, a dialog. */
const KEY_OWNERS =
	'[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"], [role="combobox"], dialog';

/** True when the key belongs to ``target`` or to an open dialog or menu (``open``: one is on the page). */
export function keyBelongsElsewhere(target: FocusTarget | null, open: boolean): boolean {
	if (open) return true;
	if (!target) return false;
	return (
		OWN_KEYS.has(target.tagName) ||
		target.isContentEditable === true ||
		target.closest(KEY_OWNERS) !== null
	);
}

/** The same question for a key event in the page. */
export function keyIsNotTheCanvas(e: KeyboardEvent): boolean {
	const target = e.target instanceof Element ? (e.target as Element & FocusTarget) : null;
	return keyBelongsElsewhere(target, document.querySelector(KEY_OWNERS) !== null);
}
