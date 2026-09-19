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
      confirmed: "bestätigt und ausgeführt",
      rejected: "abgelehnt",
    };

    function toolCallDetails(calls) {
      var wrap = document.createElement("div");
      wrap.className = "chat-tool-calls";
      (calls || []).forEach(function (call) {
        var details = document.createElement("details");
        details.className =
          "chat-tool-call" +
          (call.is_error ? " error" : "") +
          (call.status === "pending" ? " pending" : "");
        if (call.status === "pending") details.open = true;
        var summary = document.createElement("summary");
        var label = STATUS_LABELS[call.status];
        summary.textContent = "🔧 " + call.name + (label ? " – " + label : "");
        details.appendChild(summary);
        var body = document.createElement("div");
        body.className = "chat-tool-call-body";
        var argsLabel = document.createElement("div");
        argsLabel.className = "chat-tool-call-label";
        argsLabel.textContent = "Argumente";
        var args = document.createElement("pre");
        args.textContent = JSON.stringify(call.arguments, null, 2);
        var resultLabel = document.createElement("div");
        resultLabel.className = "chat-tool-call-label";
        resultLabel.textContent = "Ergebnis";
        var result = document.createElement("pre");
        result.textContent = call.result_text || "";
        body.appendChild(argsLabel);
        body.appendChild(args);
        body.appendChild(resultLabel);
        body.appendChild(result);
        details.appendChild(body);
        wrap.appendChild(details);
      });
      return wrap;
    }

    // Human-in-the-Loop: Hinweisbox mit Ausführen/Ablehnen für eine wartende Aktion.
    function pendingActionBox(message) {
      var box = document.createElement("div");
      box.className = "chat-action-confirm";
      box.dataset.messageId = message.id;
      var text = document.createElement("p");
      text.appendChild(document.createTextNode("Der Assistent möchte "));
      var code = document.createElement("code");
      code.textContent = message.pending_action.name;
      text.appendChild(code);
      text.appendChild(
        document.createTextNode(
          " ausführen. Diese Aktion ändert Daten und wird erst nach Ihrer Bestätigung ausgeführt."
        )
      );
      box.appendChild(text);
      var buttons = document.createElement("div");
      buttons.className = "chat-action-buttons";
      [
        ["confirm", "Ausführen", "primary"],
        ["reject", "Ablehnen", "secondary"],
      ].forEach(function (spec) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = spec[2];
        button.dataset.chatAction = spec[0];
        button.textContent = spec[1];
        buttons.appendChild(button);
      });
      box.appendChild(buttons);
      return box;
    }

    function buildMessageRow(message) {
      var row = document.createElement("div");
      row.className = "chat-message chat-message-" + message.role;
      row.dataset.messageId = message.id;
      var avatar = document.createElement("div");
      avatar.className = "chat-avatar";
      avatar.textContent = message.role === "user" ? "Du" : "KI";
      var bubble = document.createElement("div");
      bubble.className = "chat-bubble";
      if (message.attachments && message.attachments.length) {
        bubble.appendChild(attachmentChips(message.attachments));
      }
      if (message.tool_calls && message.tool_calls.length) {
        bubble.appendChild(toolCallDetails(message.tool_calls));
      }
      if (message.pending_action) {
        bubble.appendChild(pendingActionBox(message));
      }
      var content = document.createElement("div");
      content.className = "chat-content";
      content.textContent = message.content || "";
      bubble.appendChild(content);
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

      setSending(true);
      fetch(actionUrl(messageId, action), {
        method: "POST",
        body: data,
        headers: { Accept: "application/json" },
      })
        .then(function (response) {
          return response.json().then(function (payload) {
            return { ok: response.ok, payload: payload };
          });
        })
        .then(function (result) {
          if (!result.ok) {
            throw new Error(result.payload.error || "Unbekannter Fehler.");
          }
          replaceMessage(result.payload.updated_message);
          appendMessage(result.payload.assistant_message);
          setSending(false);
        })
        .catch(function (error) {
          box.querySelectorAll("button").forEach(function (item) {
            item.disabled = false;
          });
          setSending(false);
          showError(error.message || "Aktion fehlgeschlagen.");
        });
    });

    function setSending(active) {
      sending = active;
      pending.hidden = !active;
      // app.js deaktiviert Submit-Buttons beim Absenden – hier gezielt steuern.
      window.setTimeout(function () {
        sendButton.disabled = active;
      }, 10);
      if (active) scrollToBottom();
    }

    fileInput.addEventListener("change", function () {
      Array.prototype.forEach.call(fileInput.files, function (file) {
        pendingFiles.push(file);
      });
      fileInput.value = "";
      renderPreview();
    });

    textarea.addEventListener("input", autoresize);
    textarea.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
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

      setSending(true);
      fetch(form.action, {
        method: "POST",
        body: data,
        headers: { Accept: "application/json" },
      })
        .then(function (response) {
          return response.json().then(function (payload) {
            return { ok: response.ok, payload: payload };
          });
        })
        .then(function (result) {
          if (!result.ok) {
            throw new Error(result.payload.error || "Unbekannter Fehler.");
          }
          var payload = result.payload;
          if (payload.created_conversation) {
            var pageUrl = root.dataset.pageUrl;
            var separator = pageUrl.indexOf("?") === -1 ? "?" : "&";
            window.location =
              pageUrl + separator + "conversation_id=" + payload.conversation_id;
            return;
          }
          conversationField.value = payload.conversation_id;
          appendMessage(payload.user_message);
          appendMessage(payload.assistant_message);
          textarea.value = "";
          autoresize();
          pendingFiles = [];
          renderPreview();
          setSending(false);
          textarea.focus();
        })
        .catch(function (error) {
          setSending(false);
          showError(error.message || "Senden fehlgeschlagen.");
        });
    });

    autoresize();
    scrollToBottom();
    textarea.focus();
  });
})();
