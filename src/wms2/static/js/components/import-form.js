/**
 * Alpine importForm — handles request import form.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('importForm', () => ({
        requestName: '',
        sandboxMode: 'cmssw',
        testFraction: '',
        eventsPerJob: '',
        filesPerJob: '',
        maxFiles: '',
        dryRun: false,
        submitting: false,
        error: null,

        get isValid() {
            return this.requestName.trim().length > 0;
        },

        async submit() {
            if (!this.isValid || this.submitting) return;
            this.submitting = true;
            this.error = null;

            const body = {
                request_name: this.requestName.trim(),
                sandbox_mode: this.sandboxMode,
                dry_run: this.dryRun,
            };
            if (this.testFraction) body.test_fraction = parseFloat(this.testFraction);
            if (this.eventsPerJob) body.events_per_job = parseInt(this.eventsPerJob);
            if (this.filesPerJob) body.files_per_job = parseInt(this.filesPerJob);
            if (this.maxFiles) body.max_files = parseInt(this.maxFiles);

            try {
                const result = await WMS2_API.importRequest(body);
                window.dispatchEvent(new CustomEvent('wms2:toast', {
                    detail: { type: 'success', message: result.message }
                }));
                // Redirect to request detail
                window.location.href = '/ui/requests/' + encodeURIComponent(result.request_name);
            } catch (e) {
                this.error = e.message;
                window.dispatchEvent(new CustomEvent('wms2:toast', {
                    detail: { type: 'error', message: 'Import failed: ' + e.message }
                }));
            } finally {
                this.submitting = false;
            }
        },
    }));
});
