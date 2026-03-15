/**
 * Alpine toastManager — global toast notification stack.
 *
 * Dispatch from any component:
 *   window.dispatchEvent(new CustomEvent('condora:toast', {
 *       detail: { type: 'success', message: 'Done!' }
 *   }));
 *
 * Types: success, error, info
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('toastManager', () => ({
        toasts: [],
        _nextId: 0,

        init() {
            window.addEventListener('condora:toast', (e) => {
                this.add(e.detail.type || 'info', e.detail.message);
            });
        },

        add(type, message) {
            const id = this._nextId++;
            this.toasts.push({ id, type, message });
            // Errors stay until manually dismissed; others auto-dismiss
            if (type !== 'error') {
                setTimeout(() => this.remove(id), 5000);
            }
        },

        remove(id) {
            this.toasts = this.toasts.filter(t => t.id !== id);
        },
    }));
});
