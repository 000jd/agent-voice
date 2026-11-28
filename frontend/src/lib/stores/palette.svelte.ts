/** Command palette open state, shared so a shortcut can open it from anywhere. */
class PaletteStore {
	open = $state(false);
	query = $state('');

	toggle(): void {
		this.open = !this.open;
		if (!this.open) this.query = '';
	}

	show(): void {
		this.open = true;
	}

	hide(): void {
		this.open = false;
		this.query = '';
	}
}

export const palette = new PaletteStore();
