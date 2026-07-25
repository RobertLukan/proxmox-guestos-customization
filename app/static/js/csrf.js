// Automatically attach the CSRF token to same-origin state-changing requests.
// Reads the token from <meta name="csrf-token"> and adds the X-CSRFToken header
// to fetch() and jQuery AJAX calls, so individual pages don't have to.
(function () {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (!meta) {
        return;
    }
    var token = meta.getAttribute('content');
    var unsafe = /^(POST|PUT|PATCH|DELETE)$/i;

    // Patch window.fetch.
    if (window.fetch) {
        var originalFetch = window.fetch;
        window.fetch = function (input, init) {
            init = init || {};
            var method = init.method ||
                (input && typeof input !== 'string' ? input.method : 'GET') ||
                'GET';
            if (unsafe.test(method)) {
                var headers = new Headers(init.headers || {});
                if (!headers.has('X-CSRFToken')) {
                    headers.set('X-CSRFToken', token);
                }
                init.headers = headers;
            }
            return originalFetch.call(this, input, init);
        };
    }

    // Patch jQuery AJAX once jQuery is available (it may load after this file).
    function setupJquery() {
        if (window.jQuery) {
            window.jQuery.ajaxSetup({
                beforeSend: function (xhr, settings) {
                    if (unsafe.test(settings.type) && !settings.crossDomain) {
                        xhr.setRequestHeader('X-CSRFToken', token);
                    }
                }
            });
        }
    }
    if (window.jQuery) {
        setupJquery();
    } else {
        document.addEventListener('DOMContentLoaded', setupJquery);
    }
})();
