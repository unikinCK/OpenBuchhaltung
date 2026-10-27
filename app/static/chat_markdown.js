/* KI-Chat: kleiner Markdown-Renderer für Antworten des Assistenten.
 *
 * KI-Antworten können durch Beleg- oder Bankdaten beeinflusst sein (Prompt Injection),
 * daher wird nie HTML durchgereicht: parse() liefert einen Baum aus einfachen Objekten,
 * toDom() baut daraus ausschließlich per createElement/createTextNode DOM-Knoten.
 * Unterstützt: Absätze, Überschriften, Listen (verschachtelt), Tabellen, Zitate,
 * Codeblöcke, Trennlinien sowie fett, kursiv, durchgestrichen, `Code` und Links auf
 * App-Pfade (relativ oder absolut mit dem Ursprung der Seite, etwa Download-Links).
 * Externe Links und Bilder werden nur als Text ausgegeben.
 *
 * Läuft im Browser (window.ObhChatMarkdown) und unter Node (Tests). */
(function (root) {
  "use strict";

  var FENCE = /^\s{0,3}(```+|~~~+)\s*([\w-]*)\s*$/;
  var HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  var RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
  var LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  var QUOTE = /^\s{0,3}>\s?(.*)$/;
  var TABLE_SEPARATOR = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;
  // Beträge, Prozente, Mengen: in Tabellenspalten rechtsbündig.
  var NUMERIC_CELL = /^[-+−]?\s?[\d.,'  ]*\d[\d.,'  ]*\s?(€|EUR|%|Stk\.?)?$/;
  // Datumsangaben sehen numerisch aus, bleiben aber linksbündig.
  var DATE_CELL = /^(\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{2}-\d{2})$/;
  var ESCAPABLE = "\\`*_{}[]()#+-.!|~>";

  function isBlank(line) {
    return /^\s*$/.test(line);
  }

  function indentWidth(text) {
    var width = 0;
    for (var i = 0; i < text.length; i++) {
      if (text[i] === " ") width += 1;
      else if (text[i] === "\t") width += 4;
      else break;
    }
    return width;
  }

  // --- Inline -----------------------------------------------------------------

  function isWordChar(ch) {
    return !!ch && /[0-9A-Za-zÀ-ÖØ-öø-ÿ]/.test(ch);
  }

  function findClosing(text, delimiter, from) {
    var index = from;
    while (index < text.length) {
      var found = text.indexOf(delimiter, index);
      if (found === -1) return -1;
      if (text[found - 1] !== "\\") return found;
      index = found + 1;
    }
    return -1;
  }

  function findEmphasisClose(text, delimiter, from) {
    var index = from;
    while (index < text.length) {
      var found = findClosing(text, delimiter, index);
      if (found === -1) return -1;
      var before = text[found - 1];
      var after = text[found + delimiter.length];
      var valid = before !== undefined && !/\s/.test(before);
      if (delimiter.length === 1 && (text[found + 1] === delimiter || before === delimiter)) {
        valid = false;
      }
      if (delimiter[0] === "_" && isWordChar(after)) valid = false;
      if (valid) return found;
      index = found + delimiter.length;
    }
    return -1;
  }

  function appOrigin() {
    var page = typeof location !== "undefined" ? location : null;
    return page && /^https?:$/.test(page.protocol) ? page.origin : null;
  }

  function isSafeHref(href) {
    // Nur Pfade dieser App: kein Schema, kein protokollrelatives "//" …
    if (/^\/(?!\/)[^\s\\]*$/.test(href)) return true;
    // … oder absolute Links auf genau diesen Ursprung (Download-Links der API).
    var origin = appOrigin();
    return !!origin && href.indexOf(origin + "/") === 0 && /^[^\s\\]*$/.test(href);
  }

  function parseInline(text) {
    var nodes = [];
    var buffer = "";
    var i = 0;

    function flush() {
      if (buffer) nodes.push({ type: "text", value: buffer });
      buffer = "";
    }

    while (i < text.length) {
      var ch = text[i];
      var next = text[i + 1];

      if (ch === "\\" && next !== undefined && ESCAPABLE.indexOf(next) !== -1) {
        buffer += next;
        i += 2;
        continue;
      }

      if (ch === "\n") {
        flush();
        nodes.push({ type: "br" });
        i += 1;
        continue;
      }

      if (ch === "`") {
        var run = /^`+/.exec(text.slice(i))[0];
        var close = text.indexOf(run, i + run.length);
        if (close !== -1) {
          flush();
          nodes.push({ type: "code", value: text.slice(i + run.length, close).trim() || " " });
          i = close + run.length;
          continue;
        }
        buffer += run;
        i += run.length;
        continue;
      }

      var pair = text.slice(i, i + 2);
      if (pair === "**" || pair === "__" || pair === "~~") {
        var opensBold =
          text[i + 2] !== undefined &&
          !/\s/.test(text[i + 2]) &&
          (pair !== "__" || !isWordChar(text[i - 1]));
        var closeBold = opensBold ? findEmphasisClose(text, pair, i + 3) : -1;
        if (closeBold !== -1) {
          flush();
          nodes.push({
            type: pair === "~~" ? "del" : "strong",
            children: parseInline(text.slice(i + 2, closeBold)),
          });
          i = closeBold + 2;
          continue;
        }
        buffer += pair;
        i += 2;
        continue;
      }

      if (ch === "*" || ch === "_") {
        var opensEm =
          next !== undefined && !/\s/.test(next) && (ch !== "_" || !isWordChar(text[i - 1]));
        var closeEm = opensEm ? findEmphasisClose(text, ch, i + 1) : -1;
        if (closeEm !== -1) {
          flush();
          nodes.push({ type: "em", children: parseInline(text.slice(i + 1, closeEm)) });
          i = closeEm + 1;
          continue;
        }
        buffer += ch;
        i += 1;
        continue;
      }

      if (ch === "[" || (ch === "!" && next === "[")) {
        var start = ch === "!" ? i + 1 : i;
        var labelEnd = findClosing(text, "]", start + 1);
        if (labelEnd !== -1 && text[labelEnd + 1] === "(") {
          var hrefEnd = text.indexOf(")", labelEnd + 2);
          if (hrefEnd !== -1) {
            var label = text.slice(start + 1, labelEnd);
            var href = text.slice(labelEnd + 2, hrefEnd).trim();
            flush();
            if (ch !== "!" && isSafeHref(href)) {
              nodes.push({ type: "link", href: href, children: parseInline(label) });
            } else {
              // Externe Links/Bilder nicht anklickbar machen (kein Datenabfluss per
              // präpariertem Link), Ziel aber sichtbar lassen.
              nodes = nodes.concat(parseInline(label));
              nodes.push({ type: "text", value: " (" + href + ")" });
            }
            i = hrefEnd + 1;
            continue;
          }
        }
      }

      buffer += ch;
      i += 1;
    }
    flush();
    return nodes;
  }

  // --- Blöcke -----------------------------------------------------------------

  function splitRow(line) {
    var trimmed = line.trim();
    if (trimmed[0] === "|") trimmed = trimmed.slice(1);
    if (trimmed[trimmed.length - 1] === "|" && trimmed[trimmed.length - 2] !== "\\") {
      trimmed = trimmed.slice(0, -1);
    }
    var cells = [];
    var current = "";
    var inCode = false;
    for (var i = 0; i < trimmed.length; i++) {
      var ch = trimmed[i];
      if (ch === "\\" && trimmed[i + 1] === "|") {
        current += "|";
        i += 1;
      } else if (ch === "`") {
        inCode = !inCode;
        current += ch;
      } else if (ch === "|" && !inCode) {
        cells.push(current.trim());
        current = "";
      } else {
        current += ch;
      }
    }
    cells.push(current.trim());
    return cells;
  }

  function isTableStart(lines, index) {
    return (
      index + 1 < lines.length &&
      lines[index].indexOf("|") !== -1 &&
      TABLE_SEPARATOR.test(lines[index + 1]) &&
      lines[index + 1].indexOf("-") !== -1 &&
      (lines[index + 1].indexOf("|") !== -1 || splitRow(lines[index]).length === 1)
    );
  }

  function parseTable(lines, index) {
    var header = splitRow(lines[index]);
    var align = splitRow(lines[index + 1]).map(function (spec) {
      var left = spec[0] === ":";
      var right = spec[spec.length - 1] === ":";
      if (left && right) return "center";
      if (right) return "right";
      if (left) return "left";
      return null;
    });
    var rows = [];
    var i = index + 2;
    while (i < lines.length && !isBlank(lines[i]) && lines[i].indexOf("|") !== -1) {
      rows.push(splitRow(lines[i]));
      i += 1;
    }
    var width = header.length;
    rows = rows.map(function (row) {
      var cells = row.slice(0, width);
      while (cells.length < width) cells.push("");
      return cells;
    });
    var columns = header.map(function (_, column) {
      if (align[column]) return align[column];
      var values = rows
        .map(function (row) {
          return row[column];
        })
        .filter(function (value) {
          return value !== "" && value !== "–" && value !== "-";
        });
      var numeric =
        values.length > 0 &&
        values.every(function (value) {
          var plain = value.replace(/\*\*/g, "");
          return NUMERIC_CELL.test(plain) && !DATE_CELL.test(plain);
        });
      return numeric ? "right" : null;
    });
    return {
      block: {
        type: "table",
        align: columns,
        header: header.map(parseInline),
        rows: rows.map(function (row) {
          return row.map(parseInline);
        }),
        raw: { header: header, rows: rows },
      },
      next: i,
    };
  }

  function startsBlock(lines, index) {
    var line = lines[index];
    return (
      FENCE.test(line) ||
      HEADING.test(line) ||
      RULE.test(line) ||
      QUOTE.test(line) ||
      LIST_ITEM.test(line) ||
      isTableStart(lines, index)
    );
  }

  function parseList(lines, index) {
    var first = LIST_ITEM.exec(lines[index]);
    var baseIndent = indentWidth(first[1]);
    var ordered = /\d/.test(first[2]);
    var list = {
      type: "list",
      ordered: ordered,
      start: ordered ? parseInt(first[2], 10) : 1,
      items: [],
    };
    var i = index;
    while (i < lines.length) {
      var match = LIST_ITEM.exec(lines[i]);
      if (!match || indentWidth(match[1]) !== baseIndent || /\d/.test(match[2]) !== ordered) {
        break;
      }
      // Inhalt des Eintrags: erste Zeile plus eingerückte Folgezeilen (auch Unterlisten).
      var body = [match[3]];
      var contentIndent = baseIndent + 2;
      i += 1;
      while (i < lines.length) {
        var line = lines[i];
        if (isBlank(line)) {
          // Leerzeile beendet den Eintrag, außer es folgt eingerückter Inhalt.
          if (i + 1 < lines.length && indentWidth(lines[i + 1]) >= contentIndent &&
              !isBlank(lines[i + 1])) {
            body.push("");
            i += 1;
            continue;
          }
          break;
        }
        var nested = LIST_ITEM.exec(line);
        if (indentWidth(line) >= contentIndent) {
          body.push(line.slice(Math.min(indentWidth(line), contentIndent)));
          i += 1;
          continue;
        }
        if (nested || startsBlock(lines, i)) break;
        // Fortsetzungszeile ohne Einrückung (lazy continuation).
        body.push(line.trim());
        i += 1;
      }
      list.items.push(parseBlocks(body, true));
      // Leerzeilen zwischen Einträgen derselben Liste überspringen.
      var lookahead = i;
      while (lookahead < lines.length && isBlank(lines[lookahead])) lookahead += 1;
      var following = lookahead < lines.length ? LIST_ITEM.exec(lines[lookahead]) : null;
      if (
        lookahead !== i &&
        following &&
        indentWidth(following[1]) === baseIndent &&
        /\d/.test(following[2]) === ordered
      ) {
        i = lookahead;
      }
    }
    return { block: list, next: i };
  }

  function parseBlocks(lines, tight) {
    var blocks = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (isBlank(line)) {
        i += 1;
        continue;
      }

      var fence = FENCE.exec(line);
      if (fence) {
        var code = [];
        i += 1;
        while (i < lines.length && lines[i].trim().indexOf(fence[1]) !== 0) {
          code.push(lines[i]);
          i += 1;
        }
        blocks.push({ type: "code", language: fence[2] || "", value: code.join("\n") });
        i += 1;
        continue;
      }

      var heading = HEADING.exec(line);
      if (heading) {
        blocks.push({
          type: "heading",
          level: heading[1].length,
          children: parseInline(heading[2]),
        });
        i += 1;
        continue;
      }

      if (RULE.test(line)) {
        blocks.push({ type: "rule" });
        i += 1;
        continue;
      }

      if (isTableStart(lines, i)) {
        var table = parseTable(lines, i);
        blocks.push(table.block);
        i = table.next;
        continue;
      }

      if (LIST_ITEM.test(line)) {
        var list = parseList(lines, i);
        blocks.push(list.block);
        i = list.next;
        continue;
      }

      if (QUOTE.test(line)) {
        var quoted = [];
        while (i < lines.length && QUOTE.test(lines[i])) {
          quoted.push(QUOTE.exec(lines[i])[1]);
          i += 1;
        }
        blocks.push({ type: "quote", children: parseBlocks(quoted, false) });
        continue;
      }

      var paragraph = [line.trim()];
      i += 1;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines, i)) {
        paragraph.push(lines[i].trim());
        i += 1;
      }
      blocks.push({
        type: tight ? "plain" : "paragraph",
        children: parseInline(paragraph.join("\n")),
      });
    }
    return blocks;
  }

  function parse(text) {
    var normalized = String(text || "").replace(/\r\n?/g, "\n");
    return parseBlocks(normalized.split("\n"), false);
  }

  // --- DOM ------------------------------------------------------------------

  function inlineToDom(doc, nodes, parent) {
    nodes.forEach(function (node) {
      var element;
      switch (node.type) {
        case "text":
          parent.appendChild(doc.createTextNode(node.value));
          return;
        case "br":
          parent.appendChild(doc.createElement("br"));
          return;
        case "code":
          element = doc.createElement("code");
          element.textContent = node.value;
          break;
        case "link":
          element = doc.createElement("a");
          element.setAttribute("href", node.href);
          inlineToDom(doc, node.children, element);
          break;
        default:
          element = doc.createElement(node.type); // strong, em, del
          inlineToDom(doc, node.children, element);
      }
      parent.appendChild(element);
    });
    return parent;
  }

  function tableToTsv(raw) {
    function clean(value) {
      return value.replace(/\*\*|__|`/g, "").replace(/\t|\n/g, " ");
    }
    return [raw.header]
      .concat(raw.rows)
      .map(function (row) {
        return row.map(clean).join("\t");
      })
      .join("\n");
  }

  function tableToDom(doc, block) {
    var wrap = doc.createElement("div");
    wrap.className = "chat-md-table";
    var tools = doc.createElement("div");
    tools.className = "chat-md-table-tools";
    var copy = doc.createElement("button");
    copy.type = "button";
    copy.className = "chat-copy-button";
    copy.dataset.copyText = tableToTsv(block.raw);
    copy.dataset.copyLabel = "Tabelle kopieren";
    copy.title = "Als Tabelle kopieren (z. B. für Excel)";
    copy.textContent = "Tabelle kopieren";
    tools.appendChild(copy);
    var scroller = doc.createElement("div");
    scroller.className = "chat-md-table-scroll";
    var table = doc.createElement("table");
    var thead = doc.createElement("thead");
    var headRow = doc.createElement("tr");
    block.header.forEach(function (cell, column) {
      var th = doc.createElement("th");
      if (block.align[column]) th.className = "align-" + block.align[column];
      th.setAttribute("scope", "col");
      inlineToDom(doc, cell, th);
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = doc.createElement("tbody");
    block.rows.forEach(function (row) {
      var tr = doc.createElement("tr");
      row.forEach(function (cell, column) {
        var td = doc.createElement("td");
        if (block.align[column]) td.className = "align-" + block.align[column];
        inlineToDom(doc, cell, td);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scroller.appendChild(table);
    wrap.appendChild(scroller);
    wrap.appendChild(tools);
    return wrap;
  }

  function blocksToDom(doc, blocks, parent) {
    blocks.forEach(function (block) {
      var element;
      switch (block.type) {
        case "paragraph":
          element = inlineToDom(doc, block.children, doc.createElement("p"));
          break;
        case "plain":
          element = inlineToDom(doc, block.children, doc.createElement("div"));
          element.className = "chat-md-plain";
          break;
        case "heading":
          // h1/h2 wären im Chat zu groß: auf h3–h5 abbilden.
          element = doc.createElement("h" + Math.min(Math.max(block.level + 2, 3), 5));
          inlineToDom(doc, block.children, element);
          break;
        case "rule":
          element = doc.createElement("hr");
          break;
        case "code":
          element = doc.createElement("pre");
          var code = doc.createElement("code");
          code.textContent = block.value;
          element.appendChild(code);
          break;
        case "quote":
          element = blocksToDom(doc, block.children, doc.createElement("blockquote"));
          break;
        case "list":
          element = doc.createElement(block.ordered ? "ol" : "ul");
          if (block.ordered && block.start !== 1) element.setAttribute("start", block.start);
          block.items.forEach(function (item) {
            element.appendChild(blocksToDom(doc, item, doc.createElement("li")));
          });
          break;
        case "table":
          element = tableToDom(doc, block);
          break;
        default:
          return;
      }
      parent.appendChild(element);
    });
    return parent;
  }

  function toDom(doc, text) {
    return blocksToDom(doc, parse(text), doc.createDocumentFragment());
  }

  var api = { parse: parse, parseInline: parseInline, toDom: toDom, tableToTsv: tableToTsv };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.ObhChatMarkdown = api;
  }
})(this);
