import os
import subprocess
import sys
import tempfile
import unittest


class TestDbDoctor(unittest.TestCase):
    def test_dbdoctor_without_secret_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "sms.db")
            env = os.environ.copy()
            env.pop("SECRET_KEY", None)
            env["DATABASE_URL"] = f"sqlite:///{db_path}"

            result = subprocess.run(
                [sys.executable, "-m", "app.dbdoctor", "--print"],
                env=env,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Database file:", result.stdout)


if __name__ == "__main__":
    unittest.main()
