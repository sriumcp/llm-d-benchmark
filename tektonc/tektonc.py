#!/usr/bin/env python3
"""
tektonc — minimal render+expand for Tekton templates with loop nodes.

Authoring grammar (one construct only):
  Loop node := { loopName: str, foreach: { domain: { var: [..], ... } }, tasks: [ <task or loop>, ... ] }
  Task node := any Tekton task map (name, taskRef/taskSpec, params, runAfter, workspaces, retries, when, timeout, ...)

Semantics:
  - Expansion is cartesian over foreach.domain (keys sorted for determinism).
  - Loops can nest; variables from outer loops are in scope for inner loops.
  - Dependencies/parallelism are expressed purely via native Tekton 'runAfter'.
  - 'finally' supports the same loop nodes as 'tasks'.
  - No validation yet (name uniqueness, runAfter targets, DAG acyclicity)—add later.

CLI:
  tektonc -t pipeline.yaml.j2 -f values.yaml [-o build/pipeline.yaml] [--explain]
"""

from __future__ import annotations

import argparse
import copy
import itertools
import sys
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping

import json, yaml
from jinja2 import Environment, StrictUndefined, TemplateError, Undefined
from jinja2.runtime import Undefined as RTUndefined



# ──────────────────────────────────────────────────────────────────────────────
# Jinja helpers
#   Two-pass render:
#     - Outer env: preserves unknown loop vars (e.g., {{ modelRef|dns }} stays literal)
#     - Inner env: strict; resolves loop vars during loop expansion
# ──────────────────────────────────────────────────────────────────────────────


def _dns_inner(s: str) -> str:
    """DNS-1123-ish: lowercase, alnum and dash, trim to 63 chars with hash fallback."""
    import re, hashlib
    s2 = re.sub(r'[^a-z0-9-]+', '-', str(s).lower()).strip('-')
    if len(s2) <= 63:
        return s2
    h = hashlib.sha1(s2.encode()).hexdigest()[:8]
    return (s2[:63-1-8] + '-' + h).strip('-')

def _slug_inner(s: str) -> str:
    """Looser slug for params: keep letters/numbers/._-; replace others with '-'."""
    import re
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', str(s))

# Outer filters: if value is undefined, round-trip original expression
def _dns_outer(val: object) -> str:
    if isinstance(val, RTUndefined):
        name = getattr(val, "_undefined_name", None) or "<?>"
        return "{{ " + name + "|dns }}"
    return _dns_inner(val)  # type: ignore[arg-type]

def _slug_outer(val: object) -> str:
    if isinstance(val, RTUndefined):
        name = getattr(val, "_undefined_name", None) or "<?>"
        return "{{ " + name + "|slug }}"
    return _slug_inner(val)  # type: ignore[arg-type]

class PassthroughUndefined(Undefined):
    """
    OUTER render: keep unknown variables as their original Jinja expression,
    including dotted attributes and item access, so the INNER pass can resolve them.
      - {{ model }}            -> "{{ model }}"
      - {{ model.name }}       -> "{{ model.name }}"
      - {{ model['port'] }}    -> "{{ model['port'] }}"
      - {{ model.name|dns }}   -> dns_outer will see an Undefined and reconstruct "{{ model.name|dns }}"
    """
    __slots__ = ()

    # Compose a new Undefined that remembers the full Jinja expression text.
    def _compose(self, suffix: str) -> "PassthroughUndefined":
        base = getattr(self, "_undefined_name", None) or "?"
        expr = f"{base}{suffix}"
        # Undefined signature: (hint=None, obj=None, name=None, exc=None)
        return PassthroughUndefined(name=expr)

    # Attribute access: {{ x.y }}
    def __getattr__(self, name: str) -> "PassthroughUndefined":  # type: ignore[override]
        return self._compose(f".{name}")

    # Item access: {{ x['k'] }} / {{ x[0] }}
    def __getitem__(self, key) -> "PassthroughUndefined":  # type: ignore[override]
        # Use repr to round-trip quotes correctly
        return self._compose(f"[{repr(key)}]")

    # Function call: {{ f(x) }} -> best-effort string form
    def __call__(self, *args, **kwargs) -> "PassthroughUndefined":  # type: ignore[override]
        return self._compose("(...)")

    # Stringification -> the literal Jinja expression
    def __str__(self) -> str:  # type: ignore[override]
        name = getattr(self, "_undefined_name", None)
        return "{{ " + name + " }}" if name else "{{ ?? }}"

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False
    
def _enum(seq):
    # """Return [{i, item}, ...] for easy serial chains in Jinja."""
    # return [{"i": i, "item": v} for i, v in enumerate(seq)]
    """Return list of records with friendly neighbors for symmetric loops."""
    if seq is None:
        return []
    try:
        seq = list(seq)
    except TypeError:
        return []
    n = len(seq)
    out = []
    for i, v in enumerate(seq):
        out.append({
            "i": i,
            "item": v,
            "prev_i": i - 1 if i > 0 else -1,
            "next_i": i + 1 if i + 1 < n else None,
            "is_first": i == 0,
            "is_last": i == n - 1,
            "prev_item": seq[i - 1] if i > 0 else None,
            "next_item": seq[i + 1] if i + 1 < n else None,
        })
    return out

def build_env_outer() -> Environment:
    env = Environment(undefined=PassthroughUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)
    env.filters.update({"dns": _dns_outer, "slug": _slug_outer, "tojson": json.dumps})
    env.globals.update({"enumerate_list": _enum})
    return env

def build_env_inner() -> Environment:
    env = Environment(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)
    env.filters.update({"dns": _dns_inner, "slug": _slug_inner, "tojson": json.dumps})
    env.globals.update({"enumerate_list": _enum})
    return env


# ---------------------------------------------------------------------------
# Outer-pass protector for inline inner-Jinja blocks
# ---------------------------------------------------------------------------
_J_OPEN, _J_CLOSE = "{{", "}}"
_B_OPEN, _B_CLOSE = "{%", "%}"
_P_OPEN, _P_CLOSE = "[[", "]]"      # placeholders for {{ }}
_Q_OPEN, _Q_CLOSE = "[%", "%]"      # placeholders for {% %}

def _escape_inner_jinja_blocks(template_src: str) -> str:
    """
    Protect ONLY the lines inside a YAML literal/folded scalar whose key is
    '__jinja__' (e.g., '- __jinja__: |' or '__jinja__: >').
    We temporarily replace Jinja delimiters so the OUTER pass does not evaluate
    those blocks. The INNER pass will unescape and render them.
    """
    out_lines: list[str] = []
    in_block = False
    block_col = 0  # column where the __jinja__ key starts

    for raw in template_src.splitlines(True):  # keep endlines
        # Compute leading whitespace width (tabs expanded to 8 for consistency)
        stripped = raw.lstrip(' \t')
        leading = raw[:len(raw) - len(stripped)]
        indent_col = len(leading.expandtabs(8))

        if not in_block:
            # Detect optional list marker "- " before the key
            rest = stripped
            key_col = indent_col
            if rest.startswith('- '):
                after_dash = rest[2:]
                # count spaces after "- " before the key
                extra_ws = len(after_dash) - len(after_dash.lstrip(' \t'))
                key_col = indent_col + 2 + extra_ws
                rest = after_dash.lstrip(' \t')

            # Now look for "__jinja__" key with ":" then style indicator
            if rest.startswith('__jinja__'):
                tail = rest[len('__jinja__'):].lstrip()
                if tail.startswith(':'):
                    tail2 = tail[1:].lstrip()
                    # Literal/folded scalar starts with '|' or '>' (optionally '+' or '-')
                    if tail2 and tail2[0] in ('|', '>'):
                        # We are entering a __jinja__ scalar block
                        in_block = True
                        block_col = key_col
                        out_lines.append(raw)
                        continue

            # Not a __jinja__ scalar line; pass through
            out_lines.append(raw)
            continue

        # In a __jinja__ scalar block: YAML keeps consuming until the first
        # non-empty line whose starting column <= block_col (dedent).
        curr_col = indent_col
        if stripped == "" or curr_col > block_col:
            # Still inside the scalar → escape Jinja delimiters
            esc = (raw
                   .replace(_J_OPEN, _P_OPEN).replace(_J_CLOSE, _P_CLOSE)
                   .replace(_B_OPEN, _Q_OPEN).replace(_B_CLOSE, _Q_CLOSE))
            out_lines.append(esc)
        else:
            # Dedent — block ended before this line
            in_block = False
            out_lines.append(raw)

    # If file ends while in_block, that's fine (scalar extends to EOF)
    return "".join(out_lines)

def _unescape_inner_jinja(text: str) -> str:
    """Reverse the placeholder escaping before inner render."""
    return (text
            .replace(_P_OPEN, _J_OPEN).replace(_P_CLOSE, _J_CLOSE)
            .replace(_Q_OPEN, _B_OPEN).replace(_Q_CLOSE, _B_CLOSE))

# ──────────────────────────────────────────────────────────────────────────────
# Expander (no validation yet)
# ──────────────────────────────────────────────────────────────────────────────

def expand_document(doc: MutableMapping[str, Any],
                    globals: Mapping[str, Any] | None = None,
                    jinja_env: Environment | None = None) -> Dict[str, Any]:
    """
    Expand loops in a Pipeline document:
      - Recursively expands spec.tasks (required) and spec.finally (optional)
      - Returns a NEW dict; input is not mutated
    """
    env = jinja_env or build_env_inner()
    scope: Dict[str, Any] = dict(globals or {})

    out: Dict[str, Any] = copy.deepcopy(doc)  # type: ignore[assignment]
    spec = out.get("spec") or {}

    spec["tasks"] = expand_list(spec.get("tasks", []), scope, env)
    if "finally" in spec:
        spec["finally"] = expand_list(spec.get("finally", []), scope, env)

    out["spec"] = spec
    return out

def expand_list(nodes: Iterable[Any],
                scope: Mapping[str, Any],
                env: Environment) -> List[Dict[str, Any]]:
    """
    Core recursive expander.

    If a node is a loop node (loopName + foreach.domain + tasks list):
      * Enumerate cartesian product over the domain (keys sorted for determinism)
      * For each binding, extend scope and recursively expand the child 'tasks'
      * Concatenate all expansions

    Else (plain Tekton task):
      * Deep-copy the map; render ALL scalar strings with current scope (via Jinja)
      * Append as a single task in the flat list
    """
    flat: List[Dict[str, Any]] = []
    for node in nodes or []:
        if _is_loop_node(node):
            domain = node["foreach"]["domain"]
            child_nodes = node.get("tasks", [])
            if not isinstance(child_nodes, list):
                raise TypeError(
                    f"Loop '{node.get('loopName','<unnamed>')}' has tasks={child_nodes!r} "
                    "but a list was expected (indentation?)."
                )
            for binding in _cartesian_bindings(domain):
                child_scope = dict(scope)
                child_scope.update(binding)
                # loop-local computed variables
                loop_vars = node.get("vars")
                if isinstance(loop_vars, dict):
                    computed = {}
                    for k, v in loop_vars.items():
                        if isinstance(v, str):
                            computed[k] = env.from_string(v).render(**child_scope)
                        else:
                            computed[k] = _render_scalars(copy.deepcopy(v), child_scope, env)
                    child_scope.update(computed) 
                children = expand_list(child_nodes, child_scope, env)
                if children is None:
                    raise RuntimeError("Internal error: expand_list(child_nodes, ...) returned None")
                flat.extend(children)
        else:
            # Special case: a "virtual" task whose body is an inline Jinja block.
            if isinstance(node, dict) and "__jinja__" in node:
                block = node.get("__jinja__")
                if not isinstance(block, str):
                    raise TypeError("__jinja__ must be a YAML literal string block")
                block = _unescape_inner_jinja(block)
                try:
                    rendered_block = env.from_string(block).render(**scope)
                except TemplateError as e:
                    raise RuntimeError("Template render failed within __jinja__ block") from e
                frag = yaml.safe_load(rendered_block) or []
                if isinstance(frag, list):
                    flat.extend(frag)
                elif isinstance(frag, dict):
                    flat.append(frag)
                else:
                    raise TypeError("__jinja__ must render to a YAML list or mapping")
                continue

            # rendered = _render_scalars(copy.deepcopy(node), scope, env)
            # # After scalar render, node should be a mapping for Tekton; we pass it through
            # flat.append(rendered)  # type: ignore[arg-type]
            # Render the entire task node in one Jinja pass so {% set %} persists

            # Render the entire task node in one Jinja pass so {% set %} persists.
            # SAFETY: if the protector ever escaped outside a __jinja__ block,
            # make sure we unescape here so plain tasks evaluate correctly.
            node_text = yaml.safe_dump(copy.deepcopy(node), sort_keys=False, width=float("inf"))
            node_text = _unescape_inner_jinja(node_text)
            try:
                rendered_text = env.from_string(node_text).render(**scope)
            except TemplateError as e:
                raise RuntimeError(
                    f"Template render failed within task node (scope keys={list(scope.keys())})"
                ) from e
            rendered_node = yaml.safe_load(rendered_text) or {}
            # Allow a Jinja preamble key and drop it after render so Tekton never sees it.
            if isinstance(rendered_node, dict):
                rendered_node.pop("__jinja__", None)
            flat.append(rendered_node)  # type: ignore[arg-type]
    return flat

# ──────────────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────────────

def _is_loop_node(node: Any) -> bool:
    """A loop node must be a mapping with loopName, foreach.domain, and tasks (list)."""
    from collections.abc import Mapping as _Mapping
    if not isinstance(node, _Mapping):
        return False
    if "loopName" not in node or "foreach" not in node or "tasks" not in node:
        return False
    f = node["foreach"]
    if not isinstance(f, dict) or "domain" not in f:
        return False
    if not isinstance(node["tasks"], list):
       # Make indentation errors obvious
        raise TypeError(
           f"Loop node '{node.get('loopName','<unnamed>')}' has tasks={node.get('tasks')!r} "
           "but a list was expected. Check YAML indentation: the child task list must be "
           "indented under the loop's 'tasks:' key."
        )
        # return False
    return True

def _cartesian_bindings(domain: Mapping[str, Iterable[Any]]) -> Iterable[Dict[str, Any]]:
    """
    Deterministic cartesian enumeration of a domain dict: {var: [v1, v2], ...}
      - Sort domain keys to ensure stable order
      - Preserve the order of each value list
      - Yield dicts like {'var1': v1, 'var2': v2, ...}
    """
    if not isinstance(domain, Mapping):
        raise TypeError("foreach.domain must be a mapping of {var: list}")

    keys = sorted(domain.keys())
    lists: List[List[Any]] = []
    for k in keys:
        vals = domain[k]
        if vals is None:
            raise TypeError(f"foreach.domain['{k}'] is None; expected a list/iterable")
        if isinstance(vals, (str, bytes)):
            raise TypeError(f"foreach.domain['{k}'] must be an iterable of values (not string)")
        try:
            vals = list(vals)
        except TypeError as e:
            raise TypeError(f"foreach.domain['{k}'] is not iterable: {vals!r}") from e

        lists.append(list(vals))

    for combo in itertools.product(*lists):
        yield dict(zip(keys, combo))

def _render_scalars(obj: Any, scope: Mapping[str, Any], env: Environment) -> Any:
    """
    Recursively render scalar strings using Jinja with the given scope.
      - Dict: render values
      - List/Tuple: render each element
      - String: env.from_string(s).render(scope)
      - Other scalars: return as-is

    Note: We do NOT render dict keys — only values.
    """
    from collections.abc import Mapping as _Mapping
    if isinstance(obj, _Mapping):
        return {k: _render_scalars(v, scope, env) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render_scalars(v, scope, env) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_render_scalars(v, scope, env) for v in obj)
    if isinstance(obj, str):
        try:
            return env.from_string(obj).render(**scope)
        except TemplateError as e:
            raise RuntimeError(f"Template render failed for: {obj!r} (scope keys={list(scope.keys())})") from e
    return obj

# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Render + expand Tekton templates with loop nodes")
    ap.add_argument("-t", "--template", required=True, help="Jinja template file (use - for stdin)")
    ap.add_argument("-f", "--values",   required=True, help="YAML/JSON values file (use - for stdin)")
    ap.add_argument("-r", "--pipelinerun", required=False, help="PipelineRun definition")
    ap.add_argument("-o", "--out", help="Output YAML file (default: stdout)")
    ap.add_argument("--explain", action="store_true", help="Print name/runAfter table to stderr after expansion")
    ap.add_argument("--debug", action="store_true", help="Print full traceback and ingternal diagnostics")
    return ap.parse_args(argv)

def _read_text(path: str) -> str:
    return sys.stdin.read() if path == "-" else open(path, "r").read()

def _load_values(path: str) -> Dict[str, Any]:
    data = _read_text(path)
    return yaml.safe_load(data) or {}

def _explain(expanded: Mapping[str, Any]) -> None:
    def print_section(title: str, items: List[Mapping[str, Any]]):
        print(f"# {title}", file=sys.stderr)
        print(f"{'TASK NAME':<60}  RUNAFTER", file=sys.stderr)
        print("-" * 90, file=sys.stderr)
        for t in items:
            name = t.get("name", "<unnamed>")  # type: ignore[assignment]
            ra = t.get("runAfter", [])
            ra_str = ", ".join(ra) if isinstance(ra, list) else str(ra)
            print(f"{name:<60}  {ra_str}", file=sys.stderr)
        print("", file=sys.stderr)

    spec = expanded.get("spec") or {}
    tasks = spec.get("tasks", [])
    print_section("spec.tasks", tasks)
    if "finally" in spec:
        print_section("spec.finally", spec.get("finally", []))



def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base without losing base keys."""
    # print(">>>>>> merging override into base:")
    # print(">>>>>>>>>> base:")
    # print(json.dumps(base, indent=4))
    # print(">>>>>>>>>> override:")
    # print(json.dumps(override, indent=4))
    for k, v in override.items():
        # print(f">>>>> considering {k} and {v}")
        # print(f">>>>>>>>>> {k} in base = {k in base}")
        # if k in base:
            # print(f">>>>>>>>>> isinstance(base[k], dict) = {isinstance(base[k], dict)}")
            # print(f">>>>>>>>>> isinstance(v, dict) = {isinstance(v, dict)}")
        if (
            k in base
            and isinstance(base[k], dict)
            and isinstance(v, dict)
        ):
            # print(">>>>>>>>>> deep merging")
            base[k] = deep_merge(base[k], v)
        else:
            # print(f"setting base[{k}] to {v}")
            base[k] = v
    # print(">>>>> deep merge returning:")
    # print(json.dumps(base, indent=4))
    return base

def merge_pr(values, pr):
    if "spec" in pr and "params" in pr["spec"]:
        params = {}
        for p in pr["spec"]["params"]:
            if p["name"] in ["stack", "workload"]:
                params[p["name"]]= p["value"]
        return deep_merge(values, params)

def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        values = _load_values(args.values)
        # print(">>>>> starting values is:")
        # print(json.dumps(values, indent=4))
        if (args.pipelinerun):
            pr = _load_values(args.pipelinerun)
            values = merge_pr(values, pr)
        # print(">>>>> ending values is:")
        # print(json.dumps(values, indent=4))

        # 1) OUTER render with globals; loop vars are preserved verbatim
        env_outer = build_env_outer()
        template_src = _read_text(args.template)
        # Protect inline inner-Jinja blocks so the outer pass won't evaluate them
        template_src = _escape_inner_jinja_blocks(template_src)
        rendered = env_outer.from_string(template_src).render(**values)

        # 2) YAML parse
        doc = yaml.safe_load(rendered)
        if not isinstance(doc, dict):
            print("Rendered template is not a YAML mapping (expected a Pipeline).", file=sys.stderr)
            return 1

        # 3) Loop expansion with INNER strict env (resolves loop vars)
        env_inner = build_env_inner()
        expanded: Dict[str, Any] = expand_document(doc, globals=values, jinja_env=env_inner)

        # 4) Optional explain
        if args.explain:
            _explain(expanded)

        # 5) Output
        out_text = yaml.safe_dump(expanded, sort_keys=False, width=float("inf"))
        if args.out:
            with open(args.out, "w") as f:
                f.write(out_text)
        else:
            sys.stdout.write(out_text)
        return 0

    except TemplateError as e:
        if args.debug:
            import traceback; traceback.print_exc()
        else:
            print(f"Template render error: {e}", file=sys.stderr)
    except Exception as e:
        if args.debug:
            import traceback; traceback.print_exc()
        else:
            print(f"Error: {e}", file=sys.stderr)
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
