from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import llm_log.cli as cli


class ExpertLaunchConfigContractTests(unittest.TestCase):
    def test_parser_accepts_bounded_expert_launch_options(self) -> None:
        args = cli.parser().parse_args(
            [
                "serve",
                "--expert-service-bin",
                "/nix/store/example/bin/llm-log-expert",
                "--expert-data-dir",
                "/tmp/llm-log-expert",
                "--require-expert-plane",
            ]
        )

        self.assertEqual(
            args.expert_service_bin,
            Path("/nix/store/example/bin/llm-log-expert"),
        )
        self.assertEqual(args.expert_data_dir, Path("/tmp/llm-log-expert"))
        self.assertTrue(args.require_expert_plane)

    def test_cli_constructs_one_declared_subprocess_adapter(self) -> None:
        adapter = Mock(name="expert-adapter")

        with (
            patch.object(
                cli,
                "SubprocessExpertPlane",
                return_value=adapter,
                create=False,
            ) as adapter_cls,
            patch.object(cli, "RecorderActor", return_value=Mock()),
            patch.object(cli, "PrologClassifier", return_value=Mock()),
            patch.object(cli, "build_app", return_value=Mock()) as build_app,
            patch.object(cli.web, "run_app"),
            patch.object(
                sys,
                "argv",
                [
                    "llm-log",
                    "serve",
                    "--expert-service-bin",
                    "/opt/llm-log-expert",
                    "--expert-data-dir",
                    "/var/lib/llm-log/expert",
                    "--require-expert-plane",
                ],
            ),
        ):
            cli.main()

        adapter_cls.assert_called_once_with(
            ["/opt/llm-log-expert"],
            data_dir=Path("/var/lib/llm-log/expert"),
        )
        self.assertIs(build_app.call_args.kwargs["expert_plane"], adapter)
        self.assertTrue(build_app.call_args.kwargs["require_expert_plane"])

    def test_cli_default_does_not_construct_expert_adapter(self) -> None:
        with (
            patch.object(cli, "RecorderActor", return_value=Mock()),
            patch.object(cli, "PrologClassifier", return_value=Mock()),
            patch.object(cli, "build_app", return_value=Mock()) as build_app,
            patch.object(cli.web, "run_app"),
            patch.object(sys, "argv", ["llm-log", "serve"]),
        ):
            cli.main()

        self.assertIsNone(build_app.call_args.kwargs["expert_plane"])
        self.assertFalse(build_app.call_args.kwargs["require_expert_plane"])


if __name__ == "__main__":
    unittest.main()
