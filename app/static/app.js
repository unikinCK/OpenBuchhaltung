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
        var fragment = template.content.cloneNode(true);
        var added = Array.prototype.slice.call(fragment.children);
        container.appendChild(fragment);
        added.forEach(function (element) {
          element.querySelectorAll("[data-subledger-account]").forEach(syncPartnerSelect);
        });
        var first = added.length && added[0].querySelector("select, input");
        if (first) first.focus();
      });
    });

    // Links/Knöpfe, die eine aufklappbare Karte öffnen und dorthin springen:
    // <a href="#neue-buchung" data-open-details="#neue-buchung">
    document.querySelectorAll("[data-open-details]").forEach(function (trigger) {
      trigger.addEventListener("click", function () {
        var target = document.querySelector(trigger.dataset.openDetails);
        if (target && target.tagName === "DETAILS") target.open = true;
      });
    });
    openDetailsForHash();
    window.addEventListener("hashchange", openDetailsForHash);

    setupPartnerPanel();
    setupPartnerFill();

    // Auswahlfelder mit data-autosubmit senden ihr Formular bei Änderung ab.
    document.querySelectorAll("[data-autosubmit]").forEach(function (element) {
      element.addEventListener("change", function () {
        if (element.form) element.form.submit();
      });
    });

    document.querySelectorAll("[data-subledger-account]").forEach(syncPartnerSelect);

    // Felder nur für einen bestimmten Auswahlwert einblenden:
    // <select data-show-when="custom" data-show-target="#feld">
    document.querySelectorAll("[data-show-when]").forEach(function (select) {
      var target = document.querySelector(select.dataset.showTarget);
      if (!target) return;
      var sync = function () {
        target.hidden = select.value !== select.dataset.showWhen;
      };
      select.addEventListener("change", sync);
      sync();
    });

    document.querySelectorAll("[data-fints-lookup]").forEach(setupFintsLookup);
  });

  // FinTS-Bankzugang: BLZ in der importierten Bankenliste nachschlagen und die
  // PIN/TAN-URL vorschlagen (überschreibt keine von Hand eingetragene URL).
  // <input data-fints-lookup="/bank/fints/institut" data-fints-url-target="#url"
  //        data-fints-info="#info">
  function setupFintsLookup(input) {
    var urlField = document.querySelector(input.dataset.fintsUrlTarget);
    var info = document.querySelector(input.dataset.fintsInfo);
    var options = urlField && urlField.list;
    var autoFilled = "";
    var lastBlz = "";
    if (!urlField || !info) return;

    function show(text) {
      info.textContent = text;
    }

    function lookup() {
      var blz = input.value.replace(/\s+/g, "");
      if (blz === lastBlz) return;
      lastBlz = blz;
      if (!/^[0-9]{8}$/.test(blz)) {
        show("");
        return;
      }
      fetch(input.dataset.fintsLookup + "?blz=" + encodeURIComponent(blz), {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      })
        .then(function (response) {
          return response.json();
        })
        .then(function (data) {
          if (blz !== lastBlz) return;
          var institutes = data.institutes || [];
          var urls = data.fints_urls || [];
          if (options) {
            options.textContent = "";
            urls.forEach(function (url) {
              var option = document.createElement("option");
              option.value = url;
              options.appendChild(option);
            });
          }
          if (!institutes.length) {
            show("BLZ nicht in der FinTS-Bankenliste.");
          } else {
            var first = institutes[0];
            var label = first.name + (first.city ? ", " + first.city : "") +
              (first.bic ? " (BIC " + first.bic + ")" : "");
            if (!urls.length) {
              show(label + " — laut Bankenliste kein PIN/TAN-Zugang.");
            } else if (urls.length > 1) {
              show(label + " — mehrere FinTS-URLs, bitte eine auswählen.");
            } else {
              show(label);
            }
          }
          if (!urlField.value || urlField.value === autoFilled) {
            autoFilled = urls.length === 1 ? urls[0] : "";
            urlField.value = autoFilled;
          }
        })
        .catch(function () {
          show("");
        });
    }

    input.addEventListener("change", lookup);
    input.addEventListener("input", function () {
      if (input.value.replace(/\s+/g, "").length === 8) lookup();
    });
  }

  // Ein Anker (#id) öffnet die aufklappbare Karte, in der er liegt oder die er meint.
  function openDetailsForHash() {
    if (!window.location.hash || window.location.hash.length < 2) return;
    var target;
    try {
      target = document.querySelector(window.location.hash);
    } catch (error) {
      return;
    }
    if (!target) return;
    var details = target.tagName === "DETAILS" ? target : target.closest("details");
    while (details) {
      details.open = true;
      details = details.parentElement && details.parentElement.closest("details");
    }
  }

  // Geschäftspartner einer Buchungszeile: Auf Debitoren-/Kreditoren-Sammelkonten
  // (Nebenbuch) zeigt die Auswahl nur Kunden bzw. Lieferanten, auf allen anderen
  // Konten alle Partner (Partnerbezug). Das Feld bleibt aktiv (sonst verschieben
  // sich die Zeilenwerte beim Absenden).
  function syncPartnerSelect(accountSelect) {
    var line = accountSelect.closest(".line-group, .journal-line");
    var partnerSelect = line && line.querySelector("[data-partner-select]");
    if (!partnerSelect) return;
    var selected = accountSelect.options[accountSelect.selectedIndex];
    var subledger = selected ? selected.dataset.subledger : "";
    Array.prototype.forEach.call(partnerSelect.options, function (option) {
      if (!option.value) return;
      var allowed = !subledger ||
        (subledger === "debtor" && option.hasAttribute("data-debtor")) ||
        (subledger === "creditor" && option.hasAttribute("data-creditor"));
      option.hidden = !allowed;
      option.disabled = !allowed;
    });
    var current = partnerSelect.options[partnerSelect.selectedIndex];
    if (current && current.disabled) partnerSelect.value = "";
  }

  // Journal: „Partner zuordnen“ öffnet einen gemeinsamen Dialog und setzt das
  // Formularziel auf die gewählte Buchung. Ohne Popover-API bleibt der Link zur
  // Detailseite.
  function setupPartnerPanel() {
    var panel = document.querySelector("[data-partner-panel]");
    if (!panel || typeof panel.showPopover !== "function") return;
    var form = panel.querySelector("[data-partner-panel-form]");
    var title = panel.querySelector("[data-partner-panel-title]");
    var select = form && form.querySelector("select[name=partner_id]");
    document.querySelectorAll("[data-assign-partner]").forEach(function (link) {
      link.addEventListener("click", function (event) {
        if (!form) return;
        event.preventDefault();
        form.action = link.dataset.assignPartner;
        if (title) {
          title.textContent = link.dataset.postingNumber + " · " + (link.dataset.description || "");
        }
        if (select) select.value = "";
        panel.showPopover();
        if (select) select.focus();
      });
    });
  }

  // Buchungsdetail: einen Partner in alle Zeilenauswahlen übernehmen, soweit er
  // dort zulässig ist (Sammelkonten nur mit passender Rolle).
  function setupPartnerFill() {
    var container = document.querySelector("[data-partner-fill]");
    if (!container) return;
    var source = container.querySelector("select");
    var button = container.querySelector("[data-partner-fill-apply]");
    if (!source || !button) return;
    button.addEventListener("click", function () {
      if (!source.value) return;
      var skipped = 0;
      document.querySelectorAll("select[data-line-partner]").forEach(function (select) {
        var option = select.querySelector('option[value="' + source.value + '"]');
        if (option && !option.disabled) {
          select.value = source.value;
        } else {
          skipped += 1;
        }
      });
      if (skipped) {
        window.alert(
          skipped + " Zeile(n) nicht übernommen: Auf Sammelkonten muss die Rolle passen."
        );
      }
    });
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
