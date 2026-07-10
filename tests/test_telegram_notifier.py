import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "telegram_notifier.py"
SPEC = importlib.util.spec_from_file_location("telegram_notifier", SCRIPT)
notifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notifier)


class TelegramNotifierTests(unittest.TestCase):
    def test_config_save_is_strict_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / ".config" / "ptrak" / "telegram.json"
            with mock.patch.object(notifier, "CONFIG_PATH", path):
                notifier._save_config({"token": "secret", "chat_id": 123})
                original = path.read_text(encoding="utf-8")

                self.assertEqual(json.loads(original)["chat_id"], 123)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

                with self.assertRaises(ValueError):
                    notifier._save_config({"token": "secret", "invalid": float("nan")})
                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_config_loader_rejects_insecure_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            path = root / "telegram.json"
            path.write_text('{"token":"secret","chat_id":123}', encoding="utf-8")
            path.chmod(0o644)
            with mock.patch.object(notifier, "CONFIG_PATH", path):
                with self.assertRaisesRegex(PermissionError, "0600"):
                    notifier._load_config()

            path.chmod(0o600)
            link = root / "telegram-link.json"
            link.symlink_to(path)
            with mock.patch.object(notifier, "CONFIG_PATH", link):
                with self.assertRaisesRegex(PermissionError, "non-symlink"):
                    notifier._load_config()

    def test_error_redaction_never_emits_bot_token(self):
        message = notifier._safe_error(
            RuntimeError("https://api.telegram.org/botsecret-token/sendMessage"),
            "secret-token",
        )
        self.assertNotIn("secret-token", message)
        self.assertIn("[redacted-token]", message)


if __name__ == "__main__":
    unittest.main()
