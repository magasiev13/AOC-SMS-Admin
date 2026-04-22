import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "app"
ROUTES_PATH = APP_ROOT / "routes.py"


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _attribute_call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _messages_create_calls() -> list[tuple[str, int]]:
    callsites: list[tuple[str, int]] = []
    for path in _python_files(APP_ROOT):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "create":
                continue
            container = func.value
            if isinstance(container, ast.Attribute) and container.attr == "messages":
                callsites.append((str(path.relative_to(REPO_ROOT)), node.lineno))
    return callsites


def _outbound_send_calls(attr_name: str) -> list[tuple[str, int, bool]]:
    callsites: list[tuple[str, int, bool]] = []
    for path in _python_files(APP_ROOT):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _attribute_call_name(node) != attr_name:
                continue
            has_send_kind = any(keyword.arg == "send_kind" for keyword in node.keywords if keyword.arg)
            callsites.append((str(path.relative_to(REPO_ROOT)), node.lineno, has_send_kind))
    return callsites


class TestSendContracts(unittest.TestCase):
    def test_app_has_single_twilio_messages_create_callsite(self) -> None:
        callsites = _messages_create_calls()
        self.assertEqual(len(callsites), 1)
        self.assertEqual(callsites[0][0], "app/services/twilio_service.py")

    def test_all_send_message_calls_pass_explicit_send_kind(self) -> None:
        callsites = _outbound_send_calls("send_message")
        self.assertGreater(len(callsites), 0)
        missing = [(path, lineno) for path, lineno, has_send_kind in callsites if not has_send_kind]
        self.assertEqual(missing, [])

    def test_all_send_bulk_calls_pass_explicit_send_kind(self) -> None:
        callsites = _outbound_send_calls("send_bulk")
        self.assertGreater(len(callsites), 0)
        missing = [(path, lineno) for path, lineno, has_send_kind in callsites if not has_send_kind]
        self.assertEqual(missing, [])

    def test_routes_do_not_call_twilio_send_methods_directly(self) -> None:
        tree = _parse(ROUTES_PATH)
        violations: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            attr_name = _attribute_call_name(node)
            if attr_name in {"send_message", "send_bulk"}:
                violations.append((str(ROUTES_PATH.relative_to(REPO_ROOT)), node.lineno))
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "create"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "messages"
            ):
                violations.append((str(ROUTES_PATH.relative_to(REPO_ROOT)), node.lineno))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
