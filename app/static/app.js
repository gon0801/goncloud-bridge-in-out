// app.js — helpers JS compartidos GONCLOUD bridge.
// Cargar antes de cualquier <script> que use innerHTML con datos de API:
//   <script src="/static/app.js"></script>
//
// En competitive ya existe `escapeHtml` y `esc` en su propio app.js.
// Esta versión bridge mantiene la misma firma para que los HTMLs sean portables.

(function () {
  'use strict';

  // Escape mecánico de los 5 chars HTML especiales. Más robusto que el truco
  // textContent+innerHTML para SSR / contextos sin DOM completo.
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function (c) {
      return {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[c];
    });
  }

  // Atajo para usar en template literals: `${esc(value)}`.
  var esc = escapeHtml;

  // Exportar a window para que los <script> inline en sku_mapper.html y
  // amazon_mapper.html los puedan usar.
  window.escapeHtml = escapeHtml;
  window.esc = esc;
})();
