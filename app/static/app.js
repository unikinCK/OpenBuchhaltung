/* Gemeinsames UI-Verhalten — ersetzt Inline-Skripte und -Handler,
 * damit die CSP ohne script-src 'unsafe-inline' auskommt. */
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    // Zeilen-Vorlage anfügen (Buchungszeilen, Rechnungspositionen):
    // <button data-add-row="#container" data-template="#template">
    document.querySelectorAll("[data-add-row]").forEach(function (button) {
      var container = document.querySelector(button.dataset.addRow);
      var template = document.querySelector(button.dataset.template);
      if (!container || !template) return;
      button.addEventListener("click", function () {
        container.appendChild(template.content.cloneNode(true));
        var added = container.lastElementChild;
        if (added) added.querySelectorAll("[data-subledger-account]").forEach(syncPartnerSelect);
      });
    });

    // Auswahlfelder mit data-autosubmit senden ihr Formular bei Änderung ab.
    document.querySelectorAll("[data-autosubmit]").forEach(function (element) {
      element.addEventListener("change", function () {
        if (element.form) element.form.submit();
      });
    });

    document.querySelectorAll("[data-subledger-account]").forEach(syncPartnerSelect);
  });

  // Geschäftspartner (Nebenbuch) nur auf Sammelkonten: Die Partnerauswahl einer
  // Buchungszeile zeigt je nach Sammelkonto nur Kunden bzw. Lieferanten. Das
  // Feld bleibt aktiv (sonst verschieben sich die Zeilenwerte beim Absenden).
  function syncPartnerSelect(accountSelect) {
    var line = accountSelect.closest(".journal-line");
    var partnerSelect = line && line.querySelector("[data-partner-select]");
    if (!partnerSelect) return;
    var selected = accountSelect.options[accountSelect.selectedIndex];
    var subledger = selected ? selected.dataset.subledger : "";
    Array.prototype.forEach.call(partnerSelect.options, function (option) {
      if (!option.value) return;
      var allowed = subledger === "debtor"
        ? option.hasAttribute("data-debtor")
        : subledger === "creditor" && option.hasAttribute("data-creditor");
      option.hidden = !allowed;
      option.disabled = !allowed;
    });
    var current = partnerSelect.options[partnerSelect.selectedIndex];
    if (current && current.disabled) partnerSelect.value = "";
    partnerSelect.classList.toggle("is-muted", !subledger);
  }

  document.addEventListener("change", function (event) {
    if (event.target.matches && event.target.matches("[data-subledger-account]")) {
      syncPartnerSelect(event.target);
    }
  });

  // Sicherheitsabfrage (data-confirm am Formular oder Submit-Button) und
  // Doppel-Submit-Schutz für alle Formulare.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var source = null;
    if (form.dataset && form.dataset.confirm) {
      source = form;
    } else if (event.submitter && event.submitter.dataset.confirm) {
      source = event.submitter;
    }
    if (source && !window.confirm(source.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    // Buttons erst nach dem Tick deaktivieren, damit ein Button-Wert
    // noch mitgesendet wird.
    window.setTimeout(function () {
      form
        .querySelectorAll("button[type=submit], input[type=submit]")
        .forEach(function (button) {
          button.disabled = true;
        });
    }, 0);
  });
})();
