/** Inline toasts. `role=status` for announcements, never used for errors. */
export type ToastKind = 'info' | 'success' | 'error';

export type Toast = {
	id: number;
	kind: ToastKind;
	message: string;
	detail: string;
};

let nextId = 1;

class ToastStore {
	items = $state<Toast[]>([]);

	push(kind: ToastKind, message: string, detail = '', timeoutMs = 6000): number {
		const id = nextId++;
		this.items = [...this.items, { id, kind, message, detail }].slice(-4);
		if (timeoutMs > 0) {
			setTimeout(() => this.dismiss(id), timeoutMs);
		}
		return id;
	}

	success(message: string, detail = ''): number {
		return this.push('success', message, detail);
	}

	info(message: string, detail = ''): number {
		return this.push('info', message, detail);
	}

	error(message: string, detail = ''): number {
		return this.push('error', message, detail, 0);
	}

	dismiss(id: number): void {
		this.items = this.items.filter((item) => item.id !== id);
	}

	clear(): void {
		this.items = [];
	}
}

export const toasts = new ToastStore();
