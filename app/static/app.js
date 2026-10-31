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
    setupLineTotals();
    setupMatchJobs();

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

    // Mehrere Feldgruppen je Auswahlwert einblenden:
    // <select data-toggle-group="g"> + <div data-toggle-of="g" data-toggle-values="a b">
    document.querySelectorAll("select[data-toggle-group]").forEach(function (select) {
      var targets = document.querySelectorAll(
        '[data-toggle-of="' + select.dataset.toggleGroup + '"]'
      );
      var sync = function () {
        targets.forEach(function (target) {
          target.hidden = target.dataset.toggleValues.split(" ").indexOf(select.value) < 0;
        });
      };
      select.addEventListener("change", sync);
      sync();
    });

    // Weiterleitung zur Anmeldung bei einem fremden Anbieter (OAuth): Ein Formular
    // darf wegen CSP form-action nicht dorthin umleiten, ein Link schon.
    // <a data-auto-follow href="…">
    var autoFollow = document.querySelector("a[data-auto-follow]");
    if (autoFollow) window.location.assign(autoFollow.href);

    document.querySelectorAll("[data-fints-lookup]").forEach(setupFintsLookup);

    document.querySelectorAll("table").forEach(setupTableSort);
  });

  // Tabellen nach jeder Spalte sortieren (Klick auf den Spaltenkopf).
  // Standard: im Browser, Reihenfolge aufsteigend → absteigend → ursprünglich.
  // <table data-sort-server data-sort="date" data-sort-dir="desc">: paginierte
  // Listen sortiert der Server über ?sort=<data-sort-key>&dir=asc|desc.
  // <table data-no-sort>, <th data-no-sort>: nicht sortierbar.
  // <tr data-sort-child>: gehört zur Zeile davor; <tr data-sort-fixed>: bleibt
  // stehen (vor den übrigen Zeilen oben, sonst unten – z. B. Anfangssaldo, Summe).
  // <td data-sort-value="…">: Sortierwert statt des angezeigten Texts.
  var collator = new Intl.Collator("de", { numeric: true, sensitivity: "base" });

  function setupTableSort(table) {
    if (table.hasAttribute("data-no-sort") || !table.tHead || !table.tBodies.length) return;
    if (table.tBodies.length > 1) return; // Zeilengruppen (Buchungszeilen-Eingabe)
    var headerRow = table.tHead.rows[table.tHead.rows.length - 1];
    if (!headerRow) return;
    var server = table.hasAttribute("data-sort-server");
    var column = 0;
    Array.prototype.forEach.call(headerRow.cells, function (th) {
      var index = column;
      column += th.colSpan || 1;
      var key = th.dataset.sortKey;
      if (th.hasAttribute("data-no-sort") || !th.textContent.trim()) return;
      if (server ? !key : th.colSpan > 1) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "th-sort";
      while (th.firstChild) button.appendChild(th.firstChild);
      th.appendChild(button);
      th.classList.add("sortable");
      if (server) {
        if (table.dataset.sort === key) {
          th.setAttribute("aria-sort", table.dataset.sortDir === "desc" ? "descending" : "ascending");
        }
        button.addEventListener("click", function () {
          var url = new URL(window.location.href);
          var current = th.getAttribute("aria-sort");
          url.searchParams.set("sort", key);
          url.searchParams.set("dir", current === "ascending" ? "desc" : "asc");
          url.searchParams.delete("offset");
          url.hash = "";
          window.location.assign(url.toString());
        });
      } else {
        button.addEventListener("click", function () {
          sortTableBy(table, headerRow, th, index);
        });
      }
    });
  }

  function sortTableBy(table, headerRow, th, index) {
    var tbody = table.tBodies[0];
    var groups = rowGroups(tbody);
    if (!groups.rows.length) return;
    var state = th.getAttribute("aria-sort");
    var next = state === "ascending" ? "descending" : state === "descending" ? null : "ascending";
    Array.prototype.forEach.call(headerRow.cells, function (cell) {
      cell.removeAttribute("aria-sort");
    });
    var ordered = groups.rows.slice();
    if (next) {
      var keys = columnKeys(ordered, index);
      var direction = next === "ascending" ? 1 : -1;
      ordered.sort(function (a, b) {
        var ka = keys.get(a);
        var kb = keys.get(b);
        // Leere Zellen stehen immer am Ende.
        if (ka === null || kb === null) {
          return ka === kb ? a.position - b.position : ka === null ? 1 : -1;
        }
        var result = typeof ka === "number" ? ka - kb : collator.compare(ka, kb);
        return result * direction || a.position - b.position;
      });
      th.setAttribute("aria-sort", next);
    } else {
      ordered.sort(function (a, b) {
        return a.position - b.position;
      });
    }
    var fragment = document.createDocumentFragment();
    groups.top.concat(ordered, groups.bottom).forEach(function (group) {
      group.rows.forEach(function (row) {
        fragment.appendChild(row);
      });
    });
    tbody.appendChild(fragment);
  }

  // Zeilengruppen in Ursprungsreihenfolge (beim ersten Sortieren gemerkt).
  function rowGroups(tbody) {
    var rows = Array.prototype.slice.call(tbody.rows);
    if (rows.length && !rows[0].dataset.sortPosition) {
      rows.forEach(function (row, position) {
        row.dataset.sortPosition = String(position);
      });
    }
    rows.sort(function (a, b) {
      return a.dataset.sortPosition - b.dataset.sortPosition;
    });
    var result = { top: [], rows: [], bottom: [] };
    var current = null;
    rows.forEach(function (row) {
      if (row.hasAttribute("data-sort-child") && current) {
        current.rows.push(row);
        return;
      }
      current = { rows: [row], position: Number(row.dataset.sortPosition) };
      var lone = row.cells.length === 1 && row.cells[0].colSpan > 1;
      if (row.hasAttribute("data-sort-fixed") || lone) {
        (result.rows.length ? result.bottom : result.top).push(current);
      } else {
        result.rows.push(current);
      }
    });
    return result;
  }

  function cellAt(row, index) {
    var column = 0;
    for (var i = 0; i < row.cells.length; i++) {
      var cell = row.cells[i];
      if (index < column + (cell.colSpan || 1)) return column === index ? cell : null;
      column += cell.colSpan || 1;
    }
    return null;
  }

  // Sortierschlüssel je Gruppe: Zahl (Datum, Betrag) oder Text, leer = null.
  // Eine Spalte gilt nur als numerisch, wenn alle nichtleeren Werte es sind.
  function columnKeys(groups, index) {
    var raw = groups.map(function (group) {
      var cell = cellAt(group.rows[0], index);
      if (!cell) return "";
      if (cell.dataset.sortValue !== undefined) return cell.dataset.sortValue.trim();
      return cell.textContent.replace(/\s+/g, " ").trim();
    });
    var parsed = raw.map(parseSortValue);
    var numeric = parsed.every(function (value, i) {
      return value !== null || isEmptyText(raw[i]);
    });
    var keys = new Map();
    groups.forEach(function (group, i) {
      if (isEmptyText(raw[i])) keys.set(group, null);
      else keys.set(group, numeric ? parsed[i] : raw[i]);
    });
    return keys;
  }

  function isEmptyText(text) {
    return text === "" || text === "–" || text === "-" || text === "—";
  }

  function parseSortValue(text) {
    // Datum (TT.MM.JJJJ [HH:MM]) oder ISO-Datum am Anfang der Zelle.
    var match = /^(\d{2})\.(\d{2})\.(\d{4})(?:,? (\d{2}):(\d{2}))?/.exec(text);
    if (match) {
      return Number(match[3] + match[2] + match[1] + (match[4] || "00") + (match[5] || "00"));
    }
    match = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/.exec(text);
    if (match) {
      return Number(match[1] + match[2] + match[3] + (match[4] || "00") + (match[5] || "00"));
    }
    // Betrag/Zahl mit optionaler Einheit: 1.234,56 € · -12,5 % · 30 Tage · 19.00
    match = /^([+\-−]?)\s*([\d.]+(?:,\d+)?)\s*(?:€|EUR|%|Tage?)?$/.exec(text);
    if (!match) return null;
    var digits = match[2];
    if (digits.indexOf(",") >= 0 || /^\d{1,3}(\.\d{3})+$/.test(digits)) {
      digits = digits.replace(/\./g, "").replace(",", ".");
    }
    var number = Number(digits);
    if (isNaN(number)) return null;
    return match[1] && match[1] !== "+" ? -number : number;
  }

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

  // Buchungsmaske: Summe Soll/Haben inklusive automatischer Steuerzeilen live
  // anzeigen und Zeilen entfernen (mindestens zwei bleiben stehen).
  function parseAmount(raw) {
    var text = (raw || "").replace(/[\s€]/g, "");
    if (text.indexOf(",") >= 0) text = text.replace(/\./g, "").replace(",", ".");
    var value = parseFloat(text);
    return isNaN(value) ? 0 : value;
  }

  function formatAmount(value) {
    return value.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function setupLineTotals() {
    var output = document.querySelector("[data-line-totals]");
    var table = output && document.querySelector("#journal-lines");
    if (!output || !table) return;

    function update() {
      var debit = 0;
      var credit = 0;
      table.querySelectorAll("tbody.line-group").forEach(function (group) {
        var amount = parseAmount((group.querySelector("[name=line_amount]") || {}).value);
        var side = (group.querySelector("[name=line_side]") || {}).value;
        var tax = group.querySelector("[name=line_tax_code_id]");
        var option = tax && tax.options[tax.selectedIndex];
        var rate = option && option.dataset.rate ? parseFloat(option.dataset.rate) : 0;
        var total = amount + Math.round(amount * rate) / 100;
        if (side === "credit") credit += total; else debit += total;
      });
      var difference = Math.round((debit - credit) * 100) / 100;
      output.textContent = "Soll " + formatAmount(debit) + " · Haben " + formatAmount(credit) +
        (difference === 0 ? " · ausgeglichen" : " · Differenz " + formatAmount(difference));
      output.classList.toggle("negative", difference !== 0);
    }

    table.addEventListener("input", update);
    table.addEventListener("change", update);
    table.addEventListener("click", function (event) {
      var button = event.target.closest && event.target.closest("[data-remove-line]");
      if (!button) return;
      var groups = table.querySelectorAll("tbody.line-group");
      var group = button.closest("tbody.line-group");
      if (group && groups.length > 2) {
        group.remove();
        update();
      }
    });
    var addButton = document.querySelector('[data-add-row="#journal-lines"]');
    if (addButton) addButton.addEventListener("click", update);
    update();
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

  // Belegabgleich im Hintergrund: laufende Aufträge auf jeder Seite verfolgen und
  // bei Abschluss Bescheid geben (Browser-Benachrichtigung, sonst Hinweis oben).
  // <form data-match-start>: fragt vor dem Absenden nach der Erlaubnis für
  // Benachrichtigungen (geht nur bei einer Nutzeraktion).
  // <span data-match-job="<Status-URL>" data-match-job-file="…">: laufender Auftrag;
  // er wird in localStorage gemerkt, damit auch andere Seiten und Tabs melden.
  var MATCH_JOBS_KEY = "obh.matchJobs";
  var MATCH_JOB_POLL_MS = 5000;
  // Danach nicht mehr nachfragen (z. B. abgemeldet: Status-URL liefert die Anmeldung).
  var MATCH_JOB_MAX_AGE_MS = 30 * 60 * 1000;

  function loadMatchJobs() {
    try {
      return JSON.parse(window.localStorage.getItem(MATCH_JOBS_KEY)) || {};
    } catch (error) {
      return {};
    }
  }

  function saveMatchJobs(jobs) {
    try {
      window.localStorage.setItem(MATCH_JOBS_KEY, JSON.stringify(jobs));
    } catch (error) {
      // Ohne Speicher meldet nur die Abgleichsseite selbst.
    }
  }

  function setupMatchJobs() {
    document.querySelectorAll("form[data-match-start]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!("Notification" in window) || Notification.permission !== "default") return;
        event.preventDefault();
        var send = function () {
          form.submit();
        };
        try {
          Notification.requestPermission().then(send, send);
        } catch (error) {
          send();
        }
      });
    });

    var jobs = loadMatchJobs();
    document.querySelectorAll("[data-match-job]").forEach(function (element) {
      var url = element.dataset.matchJob;
      if (!jobs[url]) jobs[url] = { file: element.dataset.matchJobFile || "", since: Date.now() };
    });
    saveMatchJobs(jobs);
    scheduleMatchJobPoll();
  }

  function scheduleMatchJobPoll() {
    var jobs = loadMatchJobs();
    var now = Date.now();
    Object.keys(jobs).forEach(function (url) {
      if (now - (jobs[url].since || 0) > MATCH_JOB_MAX_AGE_MS) delete jobs[url];
    });
    saveMatchJobs(jobs);
    if (Object.keys(jobs).length) window.setTimeout(pollMatchJobs, MATCH_JOB_POLL_MS);
  }

  function pollMatchJobs() {
    var requests = Object.keys(loadMatchJobs()).map(function (url) {
      return fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (response) {
          if (response.status === 403 || response.status === 404) return { status: "gone" };
          var type = response.headers.get("Content-Type") || "";
          if (!response.ok || type.indexOf("application/json") === -1) return null;
          return response.json();
        })
        .then(function (job) {
          if (job && job.status !== "running") finishMatchJob(url, job);
        })
        .catch(function () {
          // Netzfehler: beim nächsten Durchlauf erneut versuchen.
        });
    });
    Promise.all(requests).then(scheduleMatchJobPoll);
  }

  function finishMatchJob(url, job) {
    var jobs = loadMatchJobs();
    var known = jobs[url];
    delete jobs[url];
    saveMatchJobs(jobs);
    // Fehlt der Eintrag, hat ein anderer Tab den Abschluss schon gemeldet.
    if (!known || job.status === "gone") return;

    var done = job.status === "done";
    var title = done ? "Belegabgleich fertig" : "Belegabgleich fehlgeschlagen";
    var detail = done
      ? job.suggestion_type === "match"
        ? "Passende Buchung gefunden – bitte prüfen und freigeben."
        : "Vorschlag für eine neue Buchung liegt zur Freigabe bereit."
      : job.error || "Unbekannter Fehler.";
    var body = (job.file_name || known.file) + ": " + detail;
    showMatchJobResult(url, job, title, body);

    if (!("Notification" in window) || Notification.permission !== "granted") return;
    try {
      var notification = new Notification(title, { body: body, tag: "obh-match-" + job.id });
      notification.onclick = function () {
        window.focus();
        window.location.href = job.page_url;
        notification.close();
      };
    } catch (error) {
      // Manche Mobilbrowser erlauben Benachrichtigungen nur über einen Service Worker.
    }
  }

  // Auf der Abgleichsseite die Zeile aktualisieren, sonst einen Hinweis oben einblenden.
  function showMatchJobResult(url, job, title, body) {
    var link = document.createElement("a");
    link.href = job.page_url;
    var badge = null;
    document.querySelectorAll("[data-match-job]").forEach(function (element) {
      if (element.dataset.matchJob === url) badge = element;
    });
    if (badge) {
      badge.className = job.status === "done" ? "badge badge-success" : "badge badge-danger";
      badge.textContent = job.status === "done" ? "Fertig" : "Fehlgeschlagen";
      link.textContent = "Seite neu laden";
      var note = badge.nextElementSibling;
      if (note) note.replaceChildren(link);
      return;
    }
    var list = document.querySelector(".flash-list");
    if (!list) {
      list = document.createElement("ul");
      list.className = "flash-list";
      document.body.insertBefore(list, document.querySelector("main"));
    }
    var item = document.createElement("li");
    item.className = job.status === "done" ? "success" : "error";
    item.textContent = title + " – " + body + " ";
    link.textContent = "Zum Belegabgleich";
    item.appendChild(link);
    list.appendChild(item);
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
