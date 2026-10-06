from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sg_rail_station_data.credentials import CredentialError, load_lta_api_key


class CredentialTests(unittest.TestCase):
    def test_process_environment_has_precedence_over_dotenv(self):
        with TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("LTA_API_KEY=local-value\n", encoding="utf-8")
            key, source = load_lta_api_key(
                environ={"LTA_API_KEY": "ci-value"}, env_path=env_file
            )
        self.assertEqual(key, "ci-value")
        self.assertEqual(source, "environment variable LTA_API_KEY")

    def test_local_dotenv_is_loaded_when_environment_is_absent(self):
        with TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "# Local secret; never commit this file.\nLTA_API_KEY='local-value'\n",
                encoding="utf-8",
            )
            key, source = load_lta_api_key(environ={}, env_path=env_file)
        self.assertEqual(key, "local-value")
        self.assertEqual(source, str(env_file.resolve()))

    def test_missing_blank_duplicate_and_unknown_entries_fail_loudly(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(CredentialError, "missing local credential file"):
                load_lta_api_key(environ={}, env_path=root / "missing")

            cases = {
                "blank": "LTA_API_KEY=\n",
                "duplicate": "LTA_API_KEY=one\nLTA_API_KEY=two\n",
                "unknown": "OTHER_SECRET=value\nLTA_API_KEY=one\n",
                "placeholder": "LTA_API_KEY=replace-with-your-datamall-account-key\n",
            }
            for name, content in cases.items():
                with self.subTest(name=name):
                    path = root / name
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(CredentialError):
                        load_lta_api_key(environ={}, env_path=path)

    def test_blank_ci_secret_does_not_fall_back_to_local_file(self):
        with TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("LTA_API_KEY=local-value\n", encoding="utf-8")
            with self.assertRaisesRegex(CredentialError, "value is blank"):
                load_lta_api_key(environ={"LTA_API_KEY": ""}, env_path=env_file)


if __name__ == "__main__":
    unittest.main()
