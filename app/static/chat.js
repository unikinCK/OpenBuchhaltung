/* KI-Chat: Senden per fetch, Anhang-Verwaltung, Nachrichten-Rendering.
 * Kein Inline-Skript (CSP: script-src 'self'). */
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("chat-root");
    var form = document.getElementById("chat-form");
    if (!root || !form) return;

    var messagesBox = document.getElementById("chat-messages");
    var welcome = document.getElementById("chat-welcome");
    var pending = document.getElementById("chat-pending");
    var textarea = document.getElementById("chat-textarea");
    var sendButton = document.getElementById("chat-send-button");
    var fileInput = document.getElementById("chat-file-input");
    var preview = document.getElementById("chat-attachment-preview");
    var errorBox = document.getElementById("chat-error");
    var conversationField = document.getElementById("chat-conversation-id");

    var pendingFiles = [];
    var sending = false;

    var markdown = window.ObhChatMarkdown;

    // Assistenten-Antworten als Markdown darstellen; Rohtext bleibt für „Kopieren“.
    function renderMarkdown(content, text) {
      content.dataset.raw = text;
      content.textContent = "";
      if (!markdown) {
        content.textContent = text;
        return;
      }
      content.classList.add("chat-markdown");
      content.appendChild(markdown.toDom(document, text));
    }

    function copyButton(text, label) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "chat-copy-button";
      button.dataset.copyLabel = label;
      button.textContent = label;
      if (text !== undefined) button.dataset.copyText = text;
      return button;
    }

    function messageFooter() {
      var footer = document.createElement("div");
      footer.className = "chat-message-tools";
      var button = copyButton(undefined, "Antwort kopieren");
      button.dataset.copySource = "message";
      footer.appendChild(button);
      return footer;
    }

    function copyText(text) {
      if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text);
      }
      // Fallback ohne Clipboard-API (z. B. HTTP im LAN).
      return new Promise(function (resolve, reject) {
        var area = document.createElement("textarea");
        area.value = text;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        var ok = false;
        try {
          ok = document.execCommand("copy");
        } catch (error) {
          ok = false;
        }
        area.remove();
        if (ok) resolve();
        else reject(new Error("Kopieren nicht möglich."));
      });
    }

    function scrollToBottom() {
      messagesBox.scrollTop = messagesBox.scrollHeight;
    }

    function autoresize() {
      // Ohne Inhalt keine Inline-Höhe: Chrome zählt sonst den umbrochenen
      // Placeholder zur scrollHeight und bläht die leere Eingabezeile auf.
      textarea.style.height = "";
      if (textarea.value) {
        textarea.style.height = Math.min(textarea.scrollHeight, 180) + "px";
      }
    }

    function showError(message) {
      errorBox.textContent = message;
      errorBox.hidden = false;
    }

    function clearError() {
      errorBox.hidden = true;
      errorBox.textContent = "";
    }

    function renderPreview() {
      preview.textContent = "";
      preview.hidden = pendingFiles.length === 0;
      pendingFiles.forEach(function (file, index) {
        var chip = document.createElement("span");
        chip.className = "chat-attachment-chip";
        chip.appendChild(document.createTextNode("📎 " + file.name));
        var remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "✕";
        remove.title = "Anhang entfernen";
        remove.addEventListener("click", function () {
          pendingFiles.splice(index, 1);
          renderPreview();
        });
        chip.appendChild(remove);
        preview.appendChild(chip);
      });
    }

    function attachmentChips(attachments) {
      var wrap = document.createElement("div");
      wrap.className = "chat-attachments";
      (attachments || []).forEach(function (meta) {
        var chip = document.createElement("span");
        chip.className =
          "chat-attachment-chip" + (meta.kind === "error" ? " error" : "");
        if (meta.error) chip.title = meta.error;
        chip.textContent = "📎 " + meta.file_name;
        wrap.appendChild(chip);
      });
      return wrap;
    }

    var STATUS_LABELS = {
      pending: "Bestätigung erforderlich",
      deferred: "zurückgestellt",
      confirmed: "ausgeführt",
      rejected: "abgelehnt",
    };

    function el(tag, className, text) {
      var node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = text;
      return node;
    }

    // Spiegelt chat.html: alle Tool-Aufrufe einer Antwort in einer zugeklappten Gruppe.
    function toolCallGroup(calls) {
      var group = el("details", "chat-tool-group");
      if (calls.some(function (call) { return call.is_error; })) {
        group.classList.add("has-error");
      }
      var summary = el("summary");
      summary.appendChild(
        el(
          "span",
          "chat-tool-group-count",
          calls.length + (calls.length === 1 ? " Schritt" : " Schritte")
        )
      );
      var titles = [];
      calls.forEach(function (call) {
        if (titles.indexOf(call.title) === -1) titles.push(call.title);
      });
      summary.appendChild(el("span", "chat-tool-group-titles", titles.join(" · ")));
      group.appendChild(summary);
      var body = el("div", "chat-tool-group-body");
      calls.forEach(function (call) {
        var details = el("details", "chat-tool-call" + (call.is_error ? " error" : ""));
        var head = el("summary");
        head.appendChild(el("span", "chat-tool-title", call.title || call.name));
        if (call.is_error) head.appendChild(el("span", "chat-tool-status status-error", "Fehler"));
        if (STATUS_LABELS[call.status]) {
          head.appendChild(
            el("span", "chat-tool-status status-" + call.status, STATUS_LABELS[call.status])
          );
        }
        head.appendChild(el("code", "chat-tool-name", call.name));
        details.appendChild(head);
        var inner = el("div", "chat-tool-call-body");
        inner.appendChild(el("div", "chat-tool-call-label", "Argumente"));
        inner.appendChild(el("pre", null, JSON.stringify(call.arguments, null, 2)));
        inner.appendChild(el("div", "chat-tool-call-label", "Ergebnis"));
        inner.appendChild(el("pre", null, call.result_display || ""));
        details.appendChild(inner);
        body.appendChild(details);
      });
      group.appendChild(body);
      return group;
    }

    function summaryTable(table) {
      var wrap = el("div", "chat-action-table");
      wrap.appendChild(el("div", "chat-tool-call-label", table.label));
      var scroller = el("div", "chat-md-table-scroll");
      var node = el("table");
      var head = el("tr");
      table.columns.forEach(function (column, index) {
        head.appendChild(el("th", table.numeric[index] ? "align-right" : null, column));
      });
      node.appendChild(el("thead")).appendChild(head);
      var body = el("tbody");
      table.rows.forEach(function (row) {
        var tr = el("tr");
        row.forEach(function (cell, index) {
          tr.appendChild(el("td", table.numeric[index] ? "align-right" : null, cell));
        });
        body.appendChild(tr);
      });
      node.appendChild(body);
      scroller.appendChild(node);
      wrap.appendChild(scroller);
      return wrap;
    }

    // Human-in-the-Loop: lesbare Zusammenfassung mit Ausführen/Ablehnen.
    function pendingActionBox(message) {
      var action = message.pending_action;
      var summary = action.summary || { fields: [], tables: [] };
      var box = el("div", "chat-action-confirm");
      box.dataset.messageId = message.id;
      var head = el("div", "chat-action-head");
      head.appendChild(el("strong", null, action.title || action.name));
      head.appendChild(el("span", "chat-action-badge", STATUS_LABELS.pending));
      box.appendChild(head);
      box.appendChild(
        el(
          "p",
          "chat-action-note",
          "Diese Aktion ändert Daten und wird erst nach Ihrer Bestätigung ausgeführt."
        )
      );
      if (summary.fields.length) {
        var fields = el("dl", "chat-action-fields");
        summary.fields.forEach(function (field) {
          fields.appendChild(el("dt", null, field[0]));
          fields.appendChild(el("dd", null, field[1]));
        });
        box.appendChild(fields);
      }
      summary.tables.forEach(function (table) {
        box.appendChild(summaryTable(table));
      });
      var buttons = el("div", "chat-action-buttons");
      [
        ["confirm", "Ausführen", "primary"],
        ["reject", "Ablehnen", "secondary"],
      ].forEach(function (spec) {
        var button = el("button", spec[2], spec[1]);
        button.type = "button";
        button.dataset.chatAction = spec[0];
        buttons.appendChild(button);
      });
      box.appendChild(buttons);
      var raw = el("details", "chat-action-raw");
      raw.appendChild(el("summary", null, "Technische Details"));
      raw.appendChild(el("code", null, action.name));
      raw.appendChild(el("pre", null, JSON.stringify(action.arguments, null, 2)));
      box.appendChild(raw);
      return box;
    }

    function buildMessageRow(message) {
      var row = document.createElement("div");
      row.className = "chat-message chat-message-" + message.role;
      row.dataset.messageId = message.id;
      var avatar = document.createElement("div");
      avatar.className = "chat-avatar";
      avatar.textContent = message.role === "user" ? "Du" : "Obi";
      if (message.role !== "user") avatar.title = "Obi";
      var bubble = document.createElement("div");
      bubble.className = "chat-bubble";
      if (message.attachments && message.attachments.length) {
        bubble.appendChild(attachmentChips(message.attachments));
      }
      if (message.tool_calls && message.tool_calls.length) {
        bubble.appendChild(toolCallGroup(message.tool_calls));
      }
      if (message.pending_action) {
        bubble.appendChild(pendingActionBox(message));
      }
      var content = document.createElement("div");
      content.className = "chat-content";
      if (message.role === "assistant") {
        renderMarkdown(content, message.content || "");
      } else {
        content.textContent = message.content || "";
      }
      bubble.appendChild(content);
      if (message.role === "assistant" && message.content) {
        bubble.appendChild(messageFooter());
      }
      row.appendChild(avatar);
      row.appendChild(bubble);
      return row;
    }

    function appendMessage(message) {
      messagesBox.insertBefore(buildMessageRow(message), pending);
      if (welcome) welcome.hidden = true;
      scrollToBottom();
    }

    function replaceMessage(message) {
      var existing = messagesBox.querySelector(
        '.chat-message[data-message-id="' + message.id + '"]'
      );
      var row = buildMessageRow(message);
      if (existing) {
        existing.replaceWith(row);
      } else {
        messagesBox.insertBefore(row, pending);
      }
    }

    function actionUrl(messageId, action) {
      // Vorlage aus url_for(..., message_id=0, action='confirm').
      return root.dataset.actionUrl.replace("/0/confirm", "/" + messageId + "/" + action);
    }

    messagesBox.addEventListener("click", function (event) {
      var copy = event.target.closest(".chat-copy-button");
      if (!copy) return;
      var text = copy.dataset.copyText;
      if (copy.dataset.copySource === "message") {
        var content = copy.closest(".chat-bubble").querySelector(".chat-content");
        text = content ? content.dataset.raw || content.textContent : "";
      }
      copyText(text || "").then(
        function () {
          copy.textContent = "Kopiert ✓";
          window.setTimeout(function () {
            copy.textContent = copy.dataset.copyLabel;
          }, 1500);
        },
        function () {
          showError("Kopieren nicht möglich – bitte Text markieren und manuell kopieren.");
        }
      );
    });

    // --- Senden mit Fortschritt (NDJSON-Stream von /chat/send bzw. /chat/actions) ---

    var progressLabel = document.getElementById("chat-progress-label");
    var progressTime = document.getElementById("chat-progress-time");
    var progressSteps = document.getElementById("chat-progress-steps");
    var progressSnippet = document.getElementById("chat-progress-snippet");
    var cancelButton = document.getElementById("chat-cancel-button");
    var controller = null;
    var timer = null;
    var lastProgressAt = 0;
    // Ab dieser Stille (s) zeigt die Anzeige, wann zuletzt etwas vom Modell kam.
    var QUIET_SECONDS = 30;

    function cancelledError() {
      var error = new Error("Antwort abgebrochen.");
      error.cancelled = true;
      return error;
    }

    // POST mit Accept: application/x-ndjson; liefert das Ergebnis aus der done-Zeile.
    // Antwortet der Server mit JSON (z. B. Validierungsfehler), wird das gelesen.
    function postChat(url, data, onProgress) {
      controller = new AbortController();
      return fetch(url, {
        method: "POST",
        body: data,
        headers: { Accept: "application/x-ndjson" },
        signal: controller.signal,
      }).then(function (response) {
        var type = response.headers.get("Content-Type") || "";
        if (type.indexOf("ndjson") === -1 || !response.body) {
          return response.json().then(function (payload) {
            if (!response.ok) throw new Error(payload.error || "Unbekannter Fehler.");
            return payload;
          });
        }
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) throw new Error("Verbindung zum Server unterbrochen.");
            buffer += decoder.decode(chunk.value, { stream: true });
            var lines = buffer.split("\n");
            buffer = lines.pop();
            for (var i = 0; i < lines.length; i++) {
              if (!lines[i].trim()) continue;
              var line = JSON.parse(lines[i]);
              if (line.type === "progress") onProgress(line.event);
              else if (line.type === "done") return line.result;
              else if (line.type === "error") throw new Error(line.error || "Fehler.");
              else if (line.type === "cancelled") throw cancelledError();
            }
            return pump();
          });
        }
        return pump();
      }).catch(function (error) {
        if (error && error.name === "AbortError") throw cancelledError();
        throw error;
      });
    }

    function setProgress(label) {
      progressLabel.textContent = label;
    }

    function setSnippet(text) {
      progressSnippet.textContent = text ? "… " + text : "";
      progressSnippet.hidden = !text;
    }

    function formatDuration(seconds) {
      return seconds < 60
        ? seconds + " s"
        : Math.floor(seconds / 60) + " min " + (seconds % 60) + " s";
    }

    function formatTokens(count) {
      return count.toLocaleString("de-DE") + (count === 1 ? " Token" : " Tokens");
    }

    function addProgressStep(title) {
      var previous = progressSteps.lastElementChild;
      if (previous) previous.classList.add("done");
      progressSteps.appendChild(el("li", null, title));
      progressSteps.hidden = false;
      scrollToBottom();
    }

    function handleProgress(event, state) {
      lastProgressAt = Date.now();
      if (event.type === "attachment") {
        setProgress("Lese Anhang „" + event.file_name + "“ …");
      } else if (event.type === "started") {
        state.started = event;
        if (!state.userShown) {
          appendMessage(event.user_message);
          state.userShown = true;
        }
      } else if (event.type === "thinking") {
        // Ein neuer Modellaufruf heißt: die Tools davor sind fertig.
        var last = progressSteps.lastElementChild;
        if (last) last.classList.add("done");
        setSnippet("");
        setProgress(event.round ? "Wertet die Ergebnisse aus …" : "Wartet auf das Modell …");
      } else if (event.type === "generating") {
        // Live-Fortschritt aus dem Modell-Stream (gedrosselt, etwa sekündlich).
        if (event.phase === "reasoning") {
          setProgress("Denkt nach … · " + formatTokens(event.tokens));
          setSnippet(event.snippet);
        } else {
          setProgress("Schreibt die Antwort … · " + formatTokens(event.tokens));
          setSnippet("");
        }
      } else if (event.type === "preparing_tool") {
        setSnippet("");
        setProgress("Bereitet „" + (event.title || event.name) + "“ vor …");
      } else if (event.type === "tool") {
        setSnippet("");
        setProgress((event.title || event.name) + " …");
        addProgressStep(event.title || event.name);
      } else if (event.type === "resolved") {
        replaceMessage(event.updated_message);
        state.resolved = true;
        cancelButton.hidden = false;
      }
    }

    function setSending(active, cancellable) {
      sending = active;
      pending.hidden = !active;
      // app.js deaktiviert Submit-Buttons beim Absenden – hier gezielt steuern.
      window.setTimeout(function () {
        sendButton.disabled = active;
      }, 10);
      window.clearInterval(timer);
      if (active) {
        // Ans Ende verschieben: neue Nachrichten erscheinen davor.
        messagesBox.appendChild(pending);
        setProgress("Sendet …");
        progressSteps.textContent = "";
        progressSteps.hidden = true;
        progressTime.textContent = "";
        setSnippet("");
        cancelButton.hidden = !cancellable;
        cancelButton.disabled = false;
        var startedAt = Date.now();
        lastProgressAt = startedAt;
        timer = window.setInterval(function () {
          var now = Date.now();
          var text = formatDuration(Math.round((now - startedAt) / 1000));
          var quiet = Math.round((now - lastProgressAt) / 1000);
          // Lange Stille (z. B. Modell liest einen langen Kontext): sichtbar machen,
          // dass die Anfrage noch läuft, aber gerade nichts Neues kommt.
          if (quiet >= QUIET_SECONDS) text += " · letzte Rückmeldung vor " + formatDuration(quiet);
          progressTime.textContent = text;
        }, 1000);
        scrollToBottom();
      } else {
        controller = null;
      }
    }

    cancelButton.addEventListener("click", function () {
      if (!controller) return;
      cancelButton.disabled = true;
      setProgress("Wird abgebrochen …");
      controller.abort();
    });

    function conversationUrl(conversationId) {
      var pageUrl = root.dataset.pageUrl;
      var separator = pageUrl.indexOf("?") === -1 ? "?" : "&";
      return pageUrl + separator + "conversation_id=" + conversationId;
    }

    function cancelledReply() {
      return { id: "cancelled-" + Date.now(), role: "assistant", content: "Antwort abgebrochen." };
    }

    messagesBox.addEventListener("click", function (event) {
      var button = event.target.closest("[data-chat-action]");
      if (!button || sending) return;
      var box = button.closest(".chat-action-confirm");
      if (!box) return;
      var messageId = box.dataset.messageId;
      var action = button.dataset.chatAction;
      clearError();
      box.querySelectorAll("button").forEach(function (item) {
        item.disabled = true;
      });

      var data = new FormData();
      var csrf = form.querySelector('input[name="_csrf_token"]');
      if (csrf) data.set("_csrf_token", csrf.value);

      // Abbrechen erst, wenn die Aktion ausgeführt ist (dann nur noch die Zusammenfassung).
      var state = {};
      setSending(true, false);
      postChat(actionUrl(messageId, action), data, function (progressEvent) {
        handleProgress(progressEvent, state);
      })
        .then(function (result) {
          replaceMessage(result.updated_message);
          appendMessage(result.assistant_message);
          setSending(false);
        })
        .catch(function (error) {
          setSending(false);
          if (error.cancelled && state.resolved) {
            appendMessage(cancelledReply());
            return;
          }
          box.querySelectorAll("button").forEach(function (item) {
            item.disabled = false;
          });
          showError(error.message || "Aktion fehlgeschlagen.");
        });
    });

    fileInput.addEventListener("change", function () {
      Array.prototype.forEach.call(fileInput.files, function (file) {
        pendingFiles.push(file);
      });
      fileInput.value = "";
      renderPreview();
    });

    textarea.addEventListener("input", autoresize);
    textarea.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit();
      }
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (sending) return;
      var text = textarea.value.trim();
      if (!text && pendingFiles.length === 0) return;
      clearError();

      var data = new FormData(form);
      data.set("message", text);
      data.delete("attachments");
      pendingFiles.forEach(function (file) {
        data.append("attachments", file, file.name);
      });

      function resetInput() {
        textarea.value = "";
        autoresize();
        pendingFiles = [];
        renderPreview();
      }

      var state = {};
      setSending(true, true);
      postChat(form.action, data, function (progressEvent) {
        handleProgress(progressEvent, state);
        if (progressEvent.type === "started") resetInput();
      })
        .then(function (payload) {
          if (payload.created_conversation) {
            window.location = conversationUrl(payload.conversation_id);
            return;
          }
          conversationField.value = payload.conversation_id;
          if (!state.userShown) appendMessage(payload.user_message);
          appendMessage(payload.assistant_message);
          resetInput();
          setSending(false);
          textarea.focus();
        })
        .catch(function (error) {
          setSending(false);
          if (error.cancelled && state.started) {
            // Der Server hält den Abbruch ebenfalls als Antwort fest.
            if (state.started.created_conversation) {
              window.location = conversationUrl(state.started.conversation_id);
              return;
            }
            appendMessage(cancelledReply());
            textarea.focus();
            return;
          }
          showError(
            error.cancelled ? "Abgebrochen." : error.message || "Senden fehlgeschlagen."
          );
        });
    });

    // Beispielfragen der Startseite direkt absenden.
    messagesBox.addEventListener("click", function (event) {
      var suggestion = event.target.closest("[data-chat-suggestion]");
      if (!suggestion || sending) return;
      textarea.value = suggestion.dataset.chatSuggestion;
      autoresize();
      form.requestSubmit();
    });

    // Seitenleiste auf schmalen Bildschirmen ein-/ausklappen.
    var sidebarToggle = document.getElementById("chat-sidebar-toggle");
    if (sidebarToggle) {
      sidebarToggle.addEventListener("click", function () {
        var open = !root.classList.contains("sidebar-open");
        root.classList.toggle("sidebar-open", open);
        sidebarToggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }

    // Unterhaltung umbenennen: Titel inline bearbeiten (Enter speichert, Esc bricht ab).
    var sidebar = document.getElementById("chat-sidebar");
    var csrfField = form.querySelector('input[name="_csrf_token"]');
    sidebar.addEventListener("click", function (event) {
      var button = event.target.closest("[data-chat-rename]");
      if (!button) return;
      var item = button.closest(".chat-conversation-item");
      var link = item.querySelector(".chat-conversation-link");
      if (item.querySelector(".chat-rename-input")) return;
      var input = el("input", "chat-rename-input");
      input.type = "text";
      input.maxLength = 80;
      input.value = link.textContent.trim();
      input.setAttribute("aria-label", "Neuer Titel");
      link.hidden = true;
      item.classList.add("renaming");
      item.insertBefore(input, link);
      input.focus();
      input.select();

      var done = false;
      function finish() {
        done = true;
        input.remove();
        link.hidden = false;
        item.classList.remove("renaming");
      }
      function save() {
        if (done) return;
        var title = input.value.trim();
        if (!title || title === link.textContent.trim()) {
          finish();
          return;
        }
        var data = new FormData();
        data.set("title", title);
        if (csrfField) data.set("_csrf_token", csrfField.value);
        input.disabled = true;
        fetch(item.dataset.renameUrl, {
          method: "POST",
          body: data,
          headers: { Accept: "application/json" },
        })
          .then(function (response) {
            return response.json().then(function (payload) {
              if (!response.ok) throw new Error(payload.error || "Umbenennen fehlgeschlagen.");
              return payload;
            });
          })
          .then(function (payload) {
            link.textContent = payload.title;
            link.title = payload.title;
            if (item.classList.contains("active")) {
              document.getElementById("chat-title").textContent = payload.title;
            }
            finish();
          })
          .catch(function (error) {
            input.disabled = false;
            input.focus();
            showError(error.message);
          });
      }
      input.addEventListener("keydown", function (keyEvent) {
        if (keyEvent.key === "Enter") {
          keyEvent.preventDefault();
          save();
        } else if (keyEvent.key === "Escape") {
          finish();
        }
      });
      input.addEventListener("blur", save);
    });

    // Vom Server gerenderte Antworten nachträglich formatieren.
    messagesBox.querySelectorAll(".chat-content[data-markdown]").forEach(function (content) {
      var text = content.textContent;
      renderMarkdown(content, text);
      if (text) content.after(messageFooter());
    });

    autoresize();
    scrollToBottom();
    textarea.focus();
  });
})();
