"""
Assertions.
A small, safe expression language for explicit expectations in .proofpush.yml, e.g.

    response.status == 201
    response.body.total == response.body.subtotal - response.body.discount
    len(response.body.items) > 0 and response.body.items[0].price >= 0
    "email" not in response.body
    vars.order_id == response.body.id

Expressions are parsed with Python's `ast` and evaluated by walking a whitelist of node types over plain JSON
data. There is no eval, no access to Python object attributes, no imports, and no calls except a few pure
functions, so a config file can't run code.
"""

from __future__ import annotations

import ast
import json
import operator
from typing import Any, Dict, List, Optional, Tuple

_COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
            ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
            ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b}
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}
_FUNCTIONS = {"len": len, "abs": abs, "round": round, "min": min, "max": max, "sum": sum}
_ROOTS = ("response", "request", "vars")
_MAX_LENGTH = 500


class AssertionSyntaxError(ValueError):
    """The expression uses something outside the assertion language."""


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


MISSING = _Missing()


def _show(value: Any) -> str:
    if value is MISSING:
        return "<missing>"
    text = json.dumps(value, default=str)
    return text if len(text) <= 60 else text[:57] + "..."


class _Evaluator:
    def __init__(self, context: Dict[str, Any]):
        self.context = context
        self.observed: List[Tuple[str, Any]] = []  # (sub-expression, value) for the failure message

    def eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return self.eval(node.body)
        if isinstance(node, ast.BoolOp):
            values = [self.eval(v) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp):
            value = self.eval(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if isinstance(node.op, ast.USub):
                return -value
            raise AssertionSyntaxError("only 'not' and '-' are allowed as unary operators")
        if isinstance(node, ast.BinOp):
            op = _BINARY.get(type(node.op))
            if not op:
                raise AssertionSyntaxError("only + - * / // % are allowed")
            return op(self.eval(node.left), self.eval(node.right))
        if isinstance(node, ast.Compare):
            left = self.eval(node.left)
            self._note(node.left, left)
            for op_node, right_node in zip(node.ops, node.comparators):
                right = self.eval(right_node)
                self._note(right_node, right)
                if left is MISSING or right is MISSING:
                    return False
                try:
                    if not _COMPARE[type(op_node)](left, right):
                        return False
                except TypeError:
                    return False
                left = right
            return True
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.eval(e) for e in node.elts]
        if isinstance(node, ast.Dict):
            return {self.eval(k): self.eval(v) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.Name):
            if node.id in ("true", "false", "null"):
                return {"true": True, "false": False, "null": None}[node.id]
            if node.id not in _ROOTS:
                raise AssertionSyntaxError(f"unknown name '{node.id}' (use response, request or vars)")
            return self.context.get(node.id, MISSING)
        if isinstance(node, ast.Attribute):
            return self._field(self.eval(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            container = self.eval(node.value)
            key = self.eval(node.slice)
            return self._field(container, key)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS or node.keywords:
                raise AssertionSyntaxError(f"only {', '.join(_FUNCTIONS)} can be called")
            args = [self.eval(a) for a in node.args]
            if any(a is MISSING for a in args):
                return MISSING
            try:
                return _FUNCTIONS[node.func.id](*args)
            except (TypeError, ValueError):
                return MISSING
        raise AssertionSyntaxError(f"'{type(node).__name__}' is not allowed in assertions")

    @staticmethod
    def _field(container: Any, key: Any) -> Any:
        """Read a key/index from JSON data only (dicts and lists): never a Python attribute."""
        if isinstance(container, dict):
            return container.get(key, MISSING)
        if isinstance(container, list) and isinstance(key, int) and not isinstance(key, bool):
            return container[key] if -len(container) <= key < len(container) else MISSING
        return MISSING

    def _note(self, node: ast.AST, value: Any) -> None:
        if not isinstance(node, ast.Constant):
            self.observed.append((ast.unparse(node), value))


def parse(expression: str) -> ast.Expression:
    """Parse and validate an assertion once (raises AssertionSyntaxError with a clear reason)."""
    if not isinstance(expression, str) or not expression.strip():
        raise AssertionSyntaxError("empty assertion")
    if len(expression) > _MAX_LENGTH:
        raise AssertionSyntaxError(f"assertion longer than {_MAX_LENGTH} characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise AssertionSyntaxError(f"not a valid expression: {exc.msg}") from None
    # Dry walk with no data: rejects disallowed constructs before any request is sent
    for node in ast.walk(tree):
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Await,
                             ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Starred, ast.JoinedStr, ast.IfExp, ast.Slice)):
            raise AssertionSyntaxError(f"'{type(node).__name__}' is not allowed in assertions")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise AssertionSyntaxError("double-underscore names are not allowed")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS):
            raise AssertionSyntaxError(f"only {', '.join(_FUNCTIONS)} can be called")
        if isinstance(node, ast.Name) and node.id not in _ROOTS + tuple(_FUNCTIONS) + ("true", "false", "null"):
            raise AssertionSyntaxError(f"unknown name '{node.id}' (use response, request or vars)")
    return tree


def check(expression: str, context: Dict[str, Any]) -> Optional[str]:
    """None if the assertion holds, else a message naming the observed values."""
    try:
        tree = parse(expression)
        evaluator = _Evaluator(context)
        ok = evaluator.eval(tree)
    except AssertionSyntaxError as exc:
        return f"invalid assertion `{expression}`: {exc}"
    except (ZeroDivisionError, OverflowError) as exc:
        return f"assertion `{expression}` could not be evaluated: {exc}"
    if ok and ok is not MISSING:
        return None
    seen = ", ".join(f"{expr} = {_show(value)}" for expr, value in evaluator.observed[:4])
    return f"assertion failed: `{expression}`" + (f" ({seen})" if seen else "")


def validate_all(expressions: List[str]) -> List[str]:
    """Syntax errors for a list of assertions (used when loading config, before anything is sent)."""
    errors = []
    for e in expressions:
        try:
            parse(e)
        except AssertionSyntaxError as exc:
            errors.append(f"`{e}`: {exc}")
    return errors
