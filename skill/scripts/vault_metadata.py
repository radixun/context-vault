"""Canonical flat frontmatter reader; bundled unchanged with the installed skill."""
import re


def frontmatter_lines(text: str) -> list[str] | None:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    return lines[1:end]


def frontmatter_syntax_problems(text: str) -> list[str]:
    """Detect the strict flat-plus-string-list YAML subset used by the vault.

    The audit intentionally has no external YAML dependency.  This checker
    rejects shapes that the small resolver and write hook cannot read
    consistently instead of silently accepting a different interpretation.
    """

    lines = frontmatter_lines(text)
    if lines is None:
        return []
    problems: list[str] = []
    seen: set[str] = set()
    parent: str | None = None
    for number, line in enumerate(lines, start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            if parent is None or not re.fullmatch(r"\s+-\s+\S.*", line):
                problems.append(
                    f"line {number}: only string-list children are supported"
                )
            continue
        parent = None
        if ":" not in line:
            problems.append(f"line {number}: expected a key/value pair")
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            problems.append(f"line {number}: invalid frontmatter key {key!r}")
            continue
        if key in seen:
            problems.append(f"line {number}: duplicate frontmatter key {key!r}")
        seen.add(key)
        value = raw_value.strip()
        if not value:
            parent = key
            continue
        if value.startswith(("|", ">", "{", "[")):
            # Inline arrays are part of the existing corpus; mappings and block
            # scalars are deliberately outside the mechanical reader contract.
            if value.startswith(("|", ">", "{")):
                problems.append(
                    f"line {number}: mappings and block scalars are unsupported"
                )
            continue
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or not value.endswith(quote):
                problems.append(f"line {number}: unterminated quoted scalar")
            continue
        # In YAML a colon followed by whitespace starts a nested mapping.  Flat
        # routing fields containing it must be quoted to remain one scalar.
        if re.search(r":\s", value):
            problems.append(
                f"line {number}: scalar containing colon-space must be quoted"
            )
    return problems


def frontmatter(text: str) -> dict[str, str] | None:
    lines = frontmatter_lines(text)
    if lines is None:
        return None
    values: dict[str, str] = {}
    current_key: str | None = None
    for line in lines:
        if line.startswith((" ", "\t")):
            if current_key and not values[current_key] and line.strip():
                values[current_key] = "<block>"
            continue
        current_key = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        values[current_key] = value.strip()
    return values


def strict_frontmatter(text: str) -> dict[str, str] | None:
    if frontmatter_syntax_problems(text):
        return None
    return frontmatter(text)
