from __future__ import annotations

import ast
import operator

SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


def calculate_expression(expression: str) -> str:
    node = ast.parse(expression, mode="eval")
    result = _evaluate(node.body)
    return str(result)


def _evaluate(node):
    if isinstance(node, ast.Num):
        return node.n
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        operator_fn = SAFE_OPERATORS[type(node.op)]
        return operator_fn(left, right)
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate(node.operand)
        operator_fn = SAFE_OPERATORS[type(node.op)]
        return operator_fn(operand)
    raise ValueError("Unsupported expression")
