import tempfile
import unittest
from pathlib import Path

from llm_log.init_config import InitConfigError, default_init_files, load_init


class InitFileTest(unittest.TestCase):
    def test_implicit_init_and_init_d_load_in_lexical_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cfg" / "llm-log"
            init_d = root / "init.d"
            init_d.mkdir(parents=True)
            (root / "init.toml").write_text(
                '[serve]\nport = 8000\n[quant_detection]\nenabled = false\n'
            )
            (init_d / "20-local.toml").write_text(
                '[serve]\nport = 9000\n[quant_detection]\nenabled = true\n'
            )
            (init_d / "10-base.toml").write_text('[serve]\nlisten = "0.0.0.0"\n')
            env = {"HOME": tmp, "XDG_CONFIG_HOME": str(Path(tmp) / "cfg")}

            state = load_init(env=env)

        self.assertEqual(state.data["serve"]["listen"], "0.0.0.0")
        self.assertEqual(state.data["serve"]["port"], 9000)
        self.assertTrue(state.data["quant_detection"]["enabled"])
        self.assertEqual([path.name for path in state.sources], [
            "init.toml",
            "10-base.toml",
            "20-local.toml",
        ])

    def test_explicit_file_loads_after_implicit_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".config" / "llm-log"
            root.mkdir(parents=True)
            (root / "init.toml").write_text('[serve]\nport = 8000\n')
            explicit = Path(tmp) / "override.toml"
            explicit.write_text('[serve]\nport = 9999\n')

            state = load_init([explicit], env={"HOME": tmp})

        self.assertEqual(state.data["serve"]["port"], 9999)

    def test_missing_explicit_init_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_init([Path(tmp) / "missing.toml"], env={"HOME": tmp})

    def test_non_toml_explicit_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "init.py"
            path.write_text("print('no executable init files')")
            with self.assertRaisesRegex(InitConfigError, "must use .toml"):
                load_init([path], env={"HOME": tmp})

    def test_default_init_files_are_empty_when_implicit_paths_do_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(default_init_files({"HOME": tmp}), ())


if __name__ == "__main__":
    unittest.main()
