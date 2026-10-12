/*
 * OpenBuchhaltung – Projekt-Webseite: Farbschema, Menü, Reiter und Kopieren.
 * Wird synchron im <head> geladen, damit das gespeicherte Farbschema vor dem
 * ersten Zeichnen gilt; alles Weitere läuft nach DOMContentLoaded.
 * Ohne JavaScript bleibt die Seite vollständig lesbar (alle Reiter untereinander).
 */
(function () {
  "use strict";

  var root = document.documentElement;
  var THEME_KEY = "obh-theme";

  root.classList.add("js");
  try {
    var stored = window.localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark") {
      root.setAttribute("data-theme", stored);
    }
  } catch (error) {
    // Kein Zugriff auf den Speicher (privates Fenster): Systemeinstellung gilt.
  }

  function systemPrefersDark() {
    return Boolean(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }

  function currentTheme() {
    var explicit = root.getAttribute("data-theme");
    if (explicit === "light" || explicit === "dark") {
      return explicit;
    }
    return systemPrefersDark() ? "dark" : "light";
  }

  function initThemeToggle() {
    var button = document.querySelector(".theme-toggle");
    if (!button) {
      return;
    }
    button.hidden = false;
    button.setAttribute("aria-label", "Dunkles Farbschema");
    var sync = function () {
      button.setAttribute("aria-pressed", currentTheme() === "dark" ? "true" : "false");
    };
    sync();
    button.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try {
        window.localStorage.setItem(THEME_KEY, next);
      } catch (error) {
        // Einstellung gilt dann nur bis zum Neuladen.
      }
      sync();
    });
    if (window.matchMedia) {
      var query = window.matchMedia("(prefers-color-scheme: dark)");
      if (query.addEventListener) {
        query.addEventListener("change", sync);
      }
    }
  }

  function initNav() {
    var toggle = document.querySelector(".nav-toggle");
    var nav = document.getElementById("site-nav");
    if (!toggle || !nav) {
      return;
    }
    var setOpen = function (open) {
      nav.classList.toggle("is-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    };
    toggle.addEventListener("click", function () {
      setOpen(!nav.classList.contains("is-open"));
    });
    nav.addEventListener("click", function (event) {
      if (event.target.closest("a")) {
        setOpen(false);
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && nav.classList.contains("is-open")) {
        setOpen(false);
        toggle.focus();
      }
    });
  }

  function initTabs() {
    var lists = document.querySelectorAll('[role="tablist"][data-tabs]');
    Array.prototype.forEach.call(lists, function (list) {
      var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
      var panels = tabs.map(function (tab) {
        return document.getElementById(tab.getAttribute("aria-controls"));
      });

      var select = function (index, moveFocus) {
        tabs.forEach(function (tab, i) {
          var active = i === index;
          tab.setAttribute("aria-selected", active ? "true" : "false");
          tab.tabIndex = active ? 0 : -1;
          if (panels[i]) {
            panels[i].hidden = !active;
          }
        });
        if (moveFocus) {
          tabs[index].focus();
        }
      };

      tabs.forEach(function (tab, i) {
        tab.addEventListener("click", function () {
          select(i, false);
        });
        tab.addEventListener("keydown", function (event) {
          var next = null;
          if (event.key === "ArrowRight") {
            next = (i + 1) % tabs.length;
          } else if (event.key === "ArrowLeft") {
            next = (i - 1 + tabs.length) % tabs.length;
          } else if (event.key === "Home") {
            next = 0;
          } else if (event.key === "End") {
            next = tabs.length - 1;
          }
          if (next !== null) {
            event.preventDefault();
            select(next, true);
          }
        });
      });

      panels.forEach(function (panel) {
        if (panel && !panel.hasAttribute("tabindex")) {
          panel.tabIndex = 0;
        }
      });

      var initial = 0;
      tabs.forEach(function (tab, i) {
        if (tab.getAttribute("aria-selected") === "true") {
          initial = i;
        }
      });
      list.hidden = false;
      select(initial, false);
    });
  }

  function selectContents(element) {
    var range = document.createRange();
    range.selectNodeContents(element);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function initCopyButtons() {
    var blocks = document.querySelectorAll(".code-block");
    Array.prototype.forEach.call(blocks, function (block) {
      var button = block.querySelector(".copy-button");
      var code = block.querySelector("pre");
      if (!button || !code) {
        return;
      }
      var timer = null;
      var report = function (label) {
        button.textContent = label;
        window.clearTimeout(timer);
        timer = window.setTimeout(function () {
          button.textContent = "Kopieren";
        }, 1800);
      };
      button.hidden = false;
      button.addEventListener("click", function () {
        var text = code.textContent;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(
            function () {
              report("Kopiert");
            },
            function () {
              selectContents(code);
              report("Markiert");
            }
          );
        } else {
          selectContents(code);
          report("Markiert");
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initThemeToggle();
    initNav();
    initTabs();
    initCopyButtons();
  });
})();
