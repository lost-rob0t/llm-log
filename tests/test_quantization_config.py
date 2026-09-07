import tempfile
import unittest
from pathlib import Path

from llm_log.cli import resolve_serve_config
from llm_log.config import ConfigError, load_config


class QuantizationPresetConfigTest(unittest.TestCase):
    def test_default_is_high_precision_and_excludes_unknown_low_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(
                Path(tmp) / "missing.toml",
                env={"HOME": tmp},
                required=False,
            )

        self.assertEqual(config.openrouter_quantization_preset, "high-precision")
        self.assertEqual(
            config.openrouter_quantizations,
            ("fp32", "fp16", "bf16", "fp8"),
        )
        self.assertNotIn("unknown", config.openrouter_quantizations)
        self.assertNotIn("fp4", config.openrouter_quantizations)
        self.assertNotIn("int4", config.openrouter_quantizations)

    def test_custom_preset_can_be_selected_from_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
version = 1
openrouter_quantization_preset = "strict"

[quantization_presets]
strict = ["fp16", "bf16"]
""".strip()
            )
            config = load_config(path, env={"HOME": tmp}, required=True)

        self.assertEqual(config.openrouter_quantization_preset, "strict")
        self.assertEqual(config.openrouter_quantizations, ("fp16", "bf16"))

    def test_unknown_precision_requires_explicit_custom_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
version = 1
openrouter_quantization_preset = "unsafe-unknown"

[quantization_presets]
unsafe-unknown = ["fp8", "unknown"]
""".strip()
            )
            config = load_config(path, env={"HOME": tmp}, required=True)

        self.assertEqual(config.openrouter_quantizations, ("fp8", "unknown"))

    def test_cli_can_switch_between_configured_presets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
version = 1
openrouter_quantization_preset = "strict"

[quantization_presets]
strict = ["fp16", "bf16"]
fast-known = ["fp8", "int8"]
""".strip()
            )
            config = resolve_serve_config(
                [
                    "serve",
                    "--config",
                    str(path),
                    "--openrouter-quantization-preset",
                    "fast-known",
                ],
                environ={"HOME": tmp},
            )

        self.assertEqual(config.openrouter_quantization_preset, "fast-known")
        self.assertEqual(config.openrouter_quantizations, ("fp8", "int8"))

    def test_active_preset_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                'version = 1\nopenrouter_quantization_preset = "missing"\n'
            )
            with self.assertRaisesRegex(ConfigError, "unknown OpenRouter quantization preset"):
                load_config(path, env={"HOME": tmp}, required=True)

    def test_unsupported_quantization_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
version = 1

[quantization_presets]
bad = ["fp16", "mystery4"]
""".strip()
            )
            with self.assertRaisesRegex(ConfigError, "unsupported value"):
                load_config(path, env={"HOME": tmp}, required=True)


if __name__ == "__main__":
    unittest.main()
