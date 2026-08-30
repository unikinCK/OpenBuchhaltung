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

    function toolCallDetails(calls) {
      var wrap = document.createElement("div");
      wrap.className = "chat-tool-calls";
      (calls || []).forEach(function (call) {
        var details = document.createElement("details");
        details.className = "chat-tool-call" + (call.is_error ? " error" : "");
        var summary = document.createElement("summary");
        summary.textContent = "🔧 " + call.name;
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

    function appendMessage(message) {
      var row = document.createElement("div");
      row.className = "chat-message chat-message-" + message.role;
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
      var content = document.createElement("div");
      content.className = "chat-content";
      content.textContent = message.content || "";
      bubble.appendChild(content);
      row.appendChild(avatar);
      row.appendChild(bubble);
      messagesBox.insertBefore(row, pending);
      if (welcome) welcome.hidden = true;
      scrollToBottom();
    }

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
