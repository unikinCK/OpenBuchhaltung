// Tests für app/static/chat_markdown.js; ausgeführt von tests/test_chat_markdown.py.
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const md = require(path.join(__dirname, "..", "..", "app", "static", "chat_markdown.js"));

function text(nodes) {
  return nodes
    .map((node) => (node.value !== undefined ? node.value : node.children ? text(node.children) : ""))
    .join("");
}

// Tabelle mit Beträgen: rechtsbündig, Datum nicht.
{
  const blocks = md.parse(
    "Treffer:\n\n| Buchung | Datum | Brutto |\n|---|---|---|\n" +
      "| 2026-0102 | 02.09.2026 | 34.744,79 € |\n| 2026-0101 | 2026-09-01 | 660,00 € |"
  );
  assert.equal(blocks[0].type, "paragraph");
  const table = blocks[1];
  assert.equal(table.type, "table");
  assert.deepEqual(table.align, [null, null, "right"]);
  assert.equal(table.rows.length, 2);
  assert.equal(text(table.rows[0][2]), "34.744,79 €");
  assert.equal(
    md.tableToTsv(table.raw),
    "Buchung\tDatum\tBrutto\n2026-0102\t02.09.2026\t34.744,79 €\n2026-0101\t2026-09-01\t660,00 €"
  );
}

// Escapter senkrechter Strich und Code in Zellen trennen keine Spalten.
{
  const [table] = md.parse("| A | B |\n|:--|--:|\n| x \\| y | `a|b` |");
  assert.deepEqual(table.align, ["left", "right"]);
  assert.equal(text(table.rows[0][0]), "x | y");
  assert.equal(table.rows[0][1][0].type, "code");
}

// Inline: fett, kursiv, Code, keine Hervorhebung in Wörtern oder bei "5 * 3".
{
  const nodes = md.parseInline("**Fiber** und *kursiv*, `x_y`, snake_case_name, 5 * 3 = 15");
  assert.equal(nodes[0].type, "strong");
  assert.equal(nodes[2].type, "em");
  assert.equal(nodes[4].type, "code");
  assert.equal(text(nodes.slice(5)), ", snake_case_name, 5 * 3 = 15");
}

// Links: nur relative App-Pfade anklickbar, externe Ziele und Bilder als Text.
{
  const nodes = md.parseInline(
    "[Beleg](/documents/5/download) [x](https://evil.example/?d=1) " +
      "[y](//evil.example) [z](javascript:alert(1)) ![b](/img.png)"
  );
  const links = nodes.filter((node) => node.type === "link");
  assert.equal(links.length, 1);
  assert.equal(links[0].href, "/documents/5/download");
  assert.match(text(nodes), /x \(https:\/\/evil\.example\/\?d=1\)/);
  assert.match(text(nodes), /z \(javascript:alert\(1/);
}

// Absolute Links nur auf den Ursprung der Seite (Download-Links der API).
{
  globalThis.location = { protocol: "https:", origin: "https://buch.example.de" };
  try {
    const nodes = md.parseInline(
      "[Download](https://buch.example.de/api/v1/downloads/abc.def) " +
        "[a](https://buch.example.de.evil.example/x) [b](https://buch.example.de@evil.example/)"
    );
    const links = nodes.filter((node) => node.type === "link");
    assert.equal(links.length, 1);
    assert.equal(links[0].href, "https://buch.example.de/api/v1/downloads/abc.def");
    assert.match(text(nodes), /a \(https:\/\/buch\.example\.de\.evil\.example\/x\)/);
  } finally {
    delete globalThis.location;
  }
}

// HTML bleibt Text.
{
  const [paragraph] = md.parse("<img src=x onerror=alert(1)> <b>fett</b>");
  assert.equal(paragraph.children.length, 1);
  assert.equal(paragraph.children[0].type, "text");
}

// Listen: nummeriert mit Unterliste, Zeilenumbrüche im Absatz.
{
  const blocks = md.parse(
    "**Möchten Sie:**\n1. Liste\n2. Details\n   - Unterpunkt\n3. Belege\n\nZeile 1\nZeile 2"
  );
  assert.equal(blocks[1].type, "list");
  assert.equal(blocks[1].ordered, true);
  assert.equal(blocks[1].items.length, 3);
  assert.equal(blocks[1].items[1][1].type, "list");
  assert.equal(blocks[2].children[1].type, "br");
}

// Liste mit Leerzeilen zwischen den Einträgen bleibt eine Liste; Startnummer.
{
  const [list] = md.parse("3. a\n\n4. b");
  assert.equal(list.items.length, 2);
  assert.equal(list.start, 3);
}

// Codeblock, Überschrift, Zitat, Trennlinie.
{
  const blocks = md.parse("## Ergebnis\n```\n**roh**\n```\n> Hinweis\n\n---");
  assert.deepEqual(
    blocks.map((block) => block.type),
    ["heading", "code", "quote", "rule"]
  );
  assert.equal(blocks[1].value, "**roh**");
}

console.log("ok");
