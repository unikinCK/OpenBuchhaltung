"""Erzeugt ein Python-Modul aus dem amtlichen Lohnsteuer-Programmablaufplan (PAP).

Das BMF veröffentlicht den PAP jedes Jahr als XML-Pseudocode mit Java-Ausdrücken
(``BigDecimal``). Dieser Generator übersetzt das XML Element für Element in Python;
die Ausdrücke bleiben unverändert und laufen gegen ``app.services.pap.runtime.BD``.
Unbekannte Konstrukte brechen die Erzeugung ab, statt still falsch zu rechnen.

Aufruf (aus dem Repo-Wurzelverzeichnis)::

    python tools/pap_codegen.py data/pap/Lohnsteuer2026.xml app/services/pap/lohnsteuer2026.py

Neues Jahr: XML nach ``data/pap/`` legen, Modul erzeugen, in
``app.services.pap.PAP_MODULES`` eintragen und gegen die BMF-Prüfwerte testen.
"""

from __future__ import annotations

import hashlib
import keyword
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ALLOWED_IDENTIFIERS = {"self", "BD", "ROUND_UP", "ROUND_DOWN", "and", "or"}
ALLOWED_METHODS = {
    "add",
    "subtract",
    "multiply",
    "divide",
    "setScale",
    "compareTo",
    "longValue",
    "valueOf",
    "ZERO",
    "ONE",
    "TEN",
}


class PapCodegenError(ValueError):
    pass


def generate(xml_path: Path) -> str:
    raw = xml_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring(raw.decode("utf-8-sig"), parser=parser)
    if root.tag != "PAP":
        raise PapCodegenError("Wurzelelement PAP fehlt.")
    pap_name = root.get("name") or xml_path.stem
    class_name = re.sub(r"\W", "", pap_name)

    inputs = _variables(root, "VARIABLES/INPUTS/INPUT")
    outputs = _variables(root, "VARIABLES/OUTPUTS/OUTPUT")
    internals = _variables(root, "VARIABLES/INTERNALS/INTERNAL")
    constants = [
        (node.get("name"), node.get("type"), node.get("value"))
        for node in root.findall("CONSTANTS/CONSTANT")
    ]
    output_groups: dict[str, list[str]] = {}
    for group in root.findall("VARIABLES/OUTPUTS"):
        output_groups[group.get("type") or "STANDARD"] = [
            node.get("name") for node in group.findall("OUTPUT")
        ]

    names = {name for name, _t, _d in inputs + outputs + internals}
    names |= {name for name, _t, _v in constants}
    methods_node = root.find("METHODS")
    if methods_node is None:
        raise PapCodegenError("METHODS fehlt.")
    method_names = {node.get("name") for node in methods_node.findall("METHOD")}
    method_names.add("MAIN")
    collision = names & method_names
    if collision:
        raise PapCodegenError(f"Namenskonflikt Variable/Methode: {sorted(collision)}")

    out: list[str] = []
    emit = out.append
    emit(f'"""Lohnsteuerberechnung nach dem amtlichen Programmablaufplan {pap_name}.')
    emit("")
    emit("Automatisch erzeugt mit ``tools/pap_codegen.py`` aus")
    emit(f"``data/pap/{xml_path.name}`` – nicht von Hand ändern, sondern neu erzeugen.")
    emit('"""')
    emit("")
    emit("# ruff: noqa: E501")
    emit("")
    emit("from __future__ import annotations")
    emit("")
    emit("from app.services.pap.runtime import BD, ROUND_DOWN, ROUND_UP  # noqa: F401")
    emit("")
    emit(f"PAP_NAME = {pap_name!r}")
    emit(f"PAP_VERSION = {root.get('versionNummer') or root.get('version') or ''!r}")
    emit(f"SOURCE_FILE = {xml_path.name!r}")
    emit(f"SOURCE_SHA256 = {digest!r}")
    emit("")
    emit("INPUTS: dict[str, str] = {")
    for name, type_, _default in inputs:
        emit(f"    {name!r}: {type_!r},")
    emit("}")
    emit("")
    emit("OUTPUTS: dict[str, tuple[str, ...]] = {")
    for group, group_names in output_groups.items():
        emit(f"    {group!r}: ({', '.join(repr(n) for n in group_names)},),")
    emit("}")
    emit("")
    emit("")
    emit(f"class {class_name}:")
    emit(f'    """PAP {pap_name}: Eingaben in Cent bzw. als Merker, Ausgaben in Cent."""')
    emit("")
    for name, type_, value in constants:
        emit(f"    {name} = {_constant_value(type_, value, names)}")
    emit("")
    emit("    def __init__(self, **inputs: object) -> None:")
    for name, type_, default in inputs + outputs + internals:
        emit(f"        self.{name} = {_default_value(type_, default, names)}")
    emit("        for name, value in inputs.items():")
    emit("            if name not in INPUTS:")
    emit('                raise ValueError(f"Unbekannter PAP-Eingabeparameter: {name}")')
    emit("            setattr(self, name, _coerce(INPUTS[name], value))")
    emit("")
    emit("    def calculate(self) -> dict[str, int]:")
    emit('        """Führt den PAP aus und liefert alle Ausgaben in Cent."""')
    emit("        self.MAIN()")
    emit("        return {")
    emit("            name: int(getattr(self, name).value)")
    emit("            for group in OUTPUTS.values()")
    emit("            for name in group")
    emit("        }")

    main = methods_node.find("MAIN")
    if main is None:
        raise PapCodegenError("MAIN fehlt.")
    _emit_method(emit, "MAIN", main, None, names, method_names)
    pending_comment: str | None = None
    for child in methods_node:
        if child.tag is ET.Comment:
            pending_comment = " ".join((child.text or "").split())
            continue
        if child.tag == "METHOD":
            _emit_method(emit, child.get("name"), child, pending_comment, names, method_names)
        pending_comment = None

    emit("")
    emit("")
    emit("def _coerce(type_: str, value: object) -> object:")
    emit('    if type_ == "int":')
    emit("        return int(value)  # type: ignore[call-overload]")
    emit('    if type_ == "double":')
    emit("        return float(value)  # type: ignore[arg-type]")
    emit("    return BD.valueOf(value)")
    emit("")
    return "\n".join(out)


def _variables(root: ET.Element, path: str) -> list[tuple[str, str, str | None]]:
    result = []
    for node in root.findall(path):
        name = node.get("name")
        if not name or keyword.iskeyword(name):
            raise PapCodegenError(f"Ungültiger Variablenname: {name!r}")
        # Das amtliche XML 2026 enthält den Tippfehler ``defaul`` (VJAHR).
        default = node.get("default", node.get("defaul"))
        result.append((name, node.get("type") or "BigDecimal", default))
    return result


def _default_value(type_: str, default: str | None, names: set[str]) -> str:
    if type_ == "int":
        return str(int(default or 0))
    if type_ == "double":
        return repr(float(default or 0))
    if type_ == "BigDecimal":
        return _expression(default or "BigDecimal.ZERO", names)
    raise PapCodegenError(f"Unbekannter Typ {type_!r}")


def _constant_value(type_: str, value: str | None, names: set[str]) -> str:
    if value is None:
        raise PapCodegenError("Konstante ohne Wert.")
    if type_ == "BigDecimal[]":
        inner = value.strip()
        if not (inner.startswith("{") and inner.endswith("}")):
            raise PapCodegenError(f"Array-Konstante unerwartet: {value[:40]}")
        return "[" + _expression(inner[1:-1], names) + "]"
    if type_ in {"BigDecimal", "int", "double"}:
        return _expression(value, names)
    raise PapCodegenError(f"Unbekannter Konstantentyp {type_!r}")


def _expression(expr: str, names: set[str]) -> str:
    text = " ".join(expr.split())
    if re.search(r"!(?!=)|\?|\+\+|--|\bnew\b|\(\s*int\s*\)|\(\s*long\s*\)", text):
        raise PapCodegenError(f"Nicht unterstützter Java-Ausdruck: {text}")
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"\bBigDecimal\.ROUND_(UP|DOWN)\b", r"ROUND_\1", text)
    if re.search(r"\bBigDecimal\.ROUND_", text):
        raise PapCodegenError(f"Nicht unterstützte Rundungsart: {text}")
    text = re.sub(r"\bBigDecimal\.", "BD.", text)

    def prefix(match: re.Match[str]) -> str:
        name = match.group(1)
        return f"self.{name}" if name in names else name

    text = re.sub(r"(?<![\w.])([A-Za-z_]\w*)\b", prefix, text)
    for match in re.finditer(r"(?<![\w.])([A-Za-z_]\w*)\b", text):
        if match.group(1) not in ALLOWED_IDENTIFIERS:
            raise PapCodegenError(f"Unbekannter Bezeichner {match.group(1)!r} in: {expr}")
    for match in re.finditer(r"\.([A-Za-z_]\w*)", text):
        name = match.group(1)
        if name not in ALLOWED_METHODS and name not in names:
            raise PapCodegenError(f"Nicht unterstützte Methode {name!r} in: {expr}")
    compile(text, "<pap>", "eval" if "=" not in re.sub(r"[<>=!]=", "", text) else "exec")
    return text


def _emit_method(emit, name, node, comment, names, method_names) -> None:
    emit("")
    emit(f"    def {name}(self) -> None:")
    if comment:
        emit(f'        """{comment.replace(chr(34) * 3, "")}"""')
    body = _block(node, 2, names, method_names)
    for line in body or ["        pass"]:
        emit(line)


def _block(node: ET.Element, depth: int, names: set[str], method_names: set[str]) -> list[str]:
    pad = "    " * depth
    lines: list[str] = []
    for child in node:
        if child.tag is ET.Comment:
            continue
        if child.tag == "EVAL":
            statement = _expression(child.get("exec") or "", names)
            match = re.match(r"^(self\.\w+)\s*=(?!=)\s*(.+)$", statement)
            if match is None:
                raise PapCodegenError(f"EVAL ist keine Zuweisung: {child.get('exec')}")
            lines.append(f"{pad}{match.group(1)} = {match.group(2)}")
        elif child.tag == "EXECUTE":
            method = child.get("method")
            if method not in method_names:
                raise PapCodegenError(f"Unbekannte Methode {method!r}")
            lines.append(f"{pad}self.{method}()")
        elif child.tag == "IF":
            condition = _expression(child.get("expr") or "", names)
            lines.append(f"{pad}if {condition}:")
            then = child.find("THEN")
            otherwise = child.find("ELSE")
            unknown = [
                c.tag for c in child if c.tag not in {"THEN", "ELSE"} and c.tag is not ET.Comment
            ]
            if unknown:
                raise PapCodegenError(f"Unerwartete Elemente in IF: {unknown}")
            then_lines = _block(then, depth + 1, names, method_names) if then is not None else []
            lines.extend(then_lines or [pad + "    pass"])
            if otherwise is not None:
                else_lines = _block(otherwise, depth + 1, names, method_names)
                if else_lines:
                    lines.append(f"{pad}else:")
                    lines.extend(else_lines)
        else:
            raise PapCodegenError(f"Unbekanntes Element {child.tag!r}")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    source, target = Path(argv[1]), Path(argv[2])
    code = generate(source)
    target.write_text(code, encoding="utf-8")
    print(f"{target} aus {source} erzeugt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
