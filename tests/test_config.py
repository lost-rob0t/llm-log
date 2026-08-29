import tempfile
import unittest
from pathlib import Path

from llm_log.cli import resolve_serve_config
from llm_log.config import default_config_path, default_data_dir, load_config


class XdgDefaultsTest(unittest.TestCase):
    def test_default_paths_follow_xdg_environment(self):
        env = {
            "HOME": "/home/tester",
            "XDG_CONFIG_HOME": "/tmp/xdg-config",
            "XDG_DATA_HOME": "/tmp/xdg-data",
        }

        self.assertEqual(default_config_path(env), Path("/tmp/xdg-config/llm-log/config.toml"))
        self.assertEqual(default_data_dir(env), Path("/tmp/xdg-data/llm-log"))

    def test_default_paths_fall_back_under_home(self):
        env = {"HOME": "/home/tester"}

        self.assertEqual(default_config_path(env), Path("/home/tester/.config/llm-log/config.toml"))
        self.assertEqual(default_data_dir(env), Path("/home/tester/.local/share/llm-log"))


class TomlConfigTest(unittest.TestCase):
    def test_config_merges_over_runtime_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
version = 1
listen = "0.0.0.0"
port = 9000
prolog_classifier = false

[upstreams]
openrouter = "https://router.example"
""".strip()
            )

            config = load_config(path, env={"HOME": "/home/tester"}, required=True)

        self.assertEqual(config.listen_address, "0.0.0.0")
        self.assertEqual(config.port, 9000)
        self.assertFalse(config.enable_prolog_classifier)
        self.assertEqual(config.upstreams["openrouter"], "https://router.example")
        self.assertEqual(config.upstreams["openai"], "https://api.openai.com")
        self.assertEqual(config.data_dir, Path("/home/tester/.local/share/llm-log"))

    def test_explicit_missing_config_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            load_config(Path("/definitely/missing/llm-log.toml"), env={"HOME": "/home/tester"}, required=True)


class CliPrecedenceTest(unittest.TestCase):
    def test_cli_overrides_file_and_upstreams_merge_per_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
version = 1
port = 9000
data_dir = "/from-file"
prolog_classifier = false

[upstreams]
openrouter = "https://file-router.example"
anthropic = "https://file-anthropic.example"
""".strip()
            )

            config = resolve_serve_config(
                [
                    "serve",
                    "--config",
                    str(path),
                    "--port",
                    "7777",
                    "--log-dir",
                    "/from-cli",
                    "--prolog-classifier",
                    "--upstream",
                    "openrouter=https://cli-router.example",
                    "--upstream",
                    "local=http://127.0.0.1:8000",
                ],
                environ={"HOME": "/home/tester"},
            )

        self.assertEqual(config.port, 7777)
        self.assertEqual(config.data_dir, Path("/from-cli"))
        self.assertTrue(config.enable_prolog_classifier)
        self.assertEqual(config.upstreams["openrouter"], "https://cli-router.example")
        self.assertEqual(config.upstreams["anthropic"], "https://file-anthropic.example")
        self.assertEqual(config.upstreams["local"], "http://127.0.0.1:8000")
        self.assertEqual(config.upstreams["openai"], "https://api.openai.com")

    def test_missing_default_config_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HOME": tmp,
                "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
                "XDG_DATA_HOME": str(Path(tmp) / "data"),
            }
            config = resolve_serve_config(["serve"], environ=env)

        self.assertEqual(config.data_dir, Path(tmp) / "data" / "llm-log")
        self.assertEqual(config.port, 8787)
        self.assertTrue(config.enable_prolog_classifier)


if __name__ == "__main__":
    unittest.main()
