/**
 * WMS2 API client — thin fetch wrapper.
 */
const WMS2_API = (() => {
    const prefix = document.querySelector('meta[name="api-prefix"]')?.content || '/api/v1';

    async function get(path) {
        const resp = await fetch(prefix + path);
        if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
        return resp.json();
    }

    return {
        status:            ()           => get('/status'),
        lifecycleStatus:   ()           => get('/lifecycle/status'),
        admissionQueue:    ()           => get('/admission/queue'),
        listRequests:      (params='')  => get('/requests' + (params ? '?' + params : '')),
        getRequest:        (name)       => get('/requests/' + encodeURIComponent(name)),
        getWorkflowByRequest: (name)    => get('/workflows/by-request/' + encodeURIComponent(name)),
        getDAG:            (id)         => get('/dags/' + encodeURIComponent(id)),
        getRequestErrors:  (name)       => get('/requests/' + encodeURIComponent(name) + '/errors'),
    };
})();
