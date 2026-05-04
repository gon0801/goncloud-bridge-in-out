// H-07/MI-21 CSRF defense — double-submit cookie pattern.
// Backend setea `csrf_token` cookie en cada respuesta; este wrapper inyecta
// `X-CSRF-Token` header en POST/PUT/DELETE/PATCH para que coincida con el
// cookie. Sin este wrapper, una página maliciosa puede triggerear estado-
// changing requests confiando en la cookie Cloudflare Access auto-attach.
(function () {
  function getCsrfToken() {
    const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  if (window.fetch) {
    const origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      init = init || {};
      const method = ((init.method) || (input && input.method) || 'GET').toUpperCase();
      if (['POST', 'PUT', 'DELETE', 'PATCH'].indexOf(method) !== -1) {
        const token = getCsrfToken();
        if (token) {
          const headers = new Headers(init.headers || (input && input.headers) || {});
          if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', token);
          init.headers = headers;
        }
      }
      return origFetch(input, init);
    };
  }

  if (window.XMLHttpRequest) {
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this._csrfMethod = (method || 'GET').toUpperCase();
      return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      if (['POST', 'PUT', 'DELETE', 'PATCH'].indexOf(this._csrfMethod || '') !== -1) {
        const token = getCsrfToken();
        if (token) {
          try { this.setRequestHeader('X-CSRF-Token', token); } catch (e) { /* noop */ }
        }
      }
      return origSend.apply(this, arguments);
    };
  }
})();
