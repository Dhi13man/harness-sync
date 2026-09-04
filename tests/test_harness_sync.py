from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ENGINE = Path(
    os.environ.get(
        "HARNESS_SYNC_ENGINE",
        Path(__file__).resolve().parents[1]
        / "skills"
        / "harness-sync"
        / "scripts"
        / "harness_sync.py",
    )
)
SPEC = importlib.util.spec_from_file_location("harness_sync_test_subject", ENGINE)
assert SPEC and SPEC.loader
harness_sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness_sync
SPEC.loader.exec_module(harness_sync)


class OhMyPiPathTests(unittest.TestCase):
    def test_symlinked_config_root_keeps_native_lexical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            backing = Path(tmp) / "backing"
            home.mkdir()
            backing.mkdir()
            (home / ".omp").symlink_to(backing, target_is_directory=True)
            with (
                patch.dict(
                    os.environ,
                    {"PI_CONFIG_DIR": ".omp", "OMP_PROFILE": "work"},
                    clear=True,
                ),
                patch.object(Path, "home", return_value=home),
            ):
                agent_home = harness_sync._omp_agent_home()

            self.assertEqual(agent_home, home / ".omp" / "profiles" / "work" / "agent")

    def test_named_profile_wins_over_default_directory_override(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "PI_CONFIG_DIR": "config-root",
                    "OMP_PROFILE": "work",
                    "PI_CODING_AGENT_DIR": "/ignored-default-agent",
                },
                clear=True,
            ),
            patch.object(Path, "home", return_value=Path(tmp)),
        ):
            self.assertEqual(
                harness_sync._omp_agent_home(),
                Path(tmp) / "config-root" / "profiles" / "work" / "agent",
            )

    def test_empty_omp_profile_selects_default_over_legacy_profile(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "OMP_PROFILE": " ",
                    "PI_PROFILE": "work",
                    "PI_CODING_AGENT_DIR": f"{tmp}/custom-agent",
                },
                clear=True,
            ),
            patch.object(Path, "home", return_value=Path(tmp)),
        ):
            self.assertEqual(harness_sync._omp_agent_home(), Path(tmp) / "custom-agent")

    def test_default_profile_ignores_legacy_profile_derived_override(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "OMP_PROFILE": "default",
                    "PI_PROFILE": "work",
                    "PI_CODING_AGENT_DIR": f"{tmp}/.omp/profiles/work/agent",
                },
                clear=True,
            ),
            patch.object(Path, "home", return_value=Path(tmp)),
        ):
            self.assertEqual(
                harness_sync._omp_agent_home(), Path(tmp) / ".omp" / "agent"
            )

    def test_invalid_profile_is_rejected(self) -> None:
        for profile in ("../escape", "CON", "com1.bak", "work.", "Work"):
            with (
                self.subTest(profile=profile),
                patch.dict(os.environ, {"OMP_PROFILE": profile}, clear=True),
            ):
                with self.assertRaisesRegex(ValueError, "invalid OMP profile"):
                    harness_sync._omp_agent_home()

    def test_windows_drive_qualified_config_root_is_rejected(self) -> None:
        with (
            patch.dict(os.environ, {"PI_CONFIG_DIR": "D:\\outside"}, clear=True),
            patch.object(os, "name", "nt"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid OMP config directory"):
                harness_sync._omp_agent_home()

    @unittest.skipIf(os.name == "nt", "POSIX path semantics")
    def test_posix_drive_like_config_name_remains_relative(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"PI_CONFIG_DIR": "C:config"}, clear=True),
            patch.object(os, "name", "posix"),
            patch.object(Path, "home", return_value=Path(tmp)),
        ):
            self.assertEqual(
                harness_sync._omp_agent_home(),
                Path(tmp) / "C:config" / "agent",
            )


class PiAndCursorPathTests(unittest.TestCase):
    def test_pi_uses_native_default_and_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(Path, "home", return_value=root),
            ):
                self.assertEqual(harness_sync._pi_agent_home(), root / ".pi" / "agent")
            with patch.dict(
                os.environ,
                {"PI_CODING_AGENT_DIR": str(root / "custom-pi")},
                clear=True,
            ):
                self.assertEqual(harness_sync._pi_agent_home(), root / "custom-pi")

    def test_cursor_cli_config_honors_explicit_and_xdg_locations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.dict(
                    os.environ,
                    {
                        "CURSOR_CONFIG_DIR": str(root / "explicit"),
                        "XDG_CONFIG_HOME": str(root / "xdg"),
                    },
                    clear=True,
                ),
                patch.object(sys, "platform", "linux"),
            ):
                self.assertEqual(
                    harness_sync._cursor_cli_config_path(),
                    root / "explicit" / "cli-config.json",
                )
            with (
                patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": str(root / "xdg")},
                    clear=True,
                ),
                patch.object(sys, "platform", "linux"),
            ):
                self.assertEqual(
                    harness_sync._cursor_cli_config_path(),
                    root / "xdg" / "cursor" / "cli-config.json",
                )


class ProjectionSafetyTests(unittest.TestCase):
    def test_windows_omp_mutex_clears_stale_last_error(self) -> None:
        error = {"value": 183}
        cleared: list[int] = []
        closed: list[int] = []

        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.callback(*args)

        create_mutex = FakeFunction(lambda *_: 123)
        close_handle = FakeFunction(lambda handle: closed.append(handle) or 1)
        kernel32 = SimpleNamespace(
            CreateMutexW=create_mutex,
            CloseHandle=close_handle,
        )

        def set_last_error(value: int) -> None:
            error["value"] = value
            cleared.append(value)

        fake_ctypes = SimpleNamespace(
            WinDLL=lambda *_args, **_kwargs: kernel32,
            WinError=lambda value: OSError(value),
            c_void_p=object,
            c_int=int,
            c_wchar_p=str,
            get_last_error=lambda: error["value"],
            set_last_error=set_last_error,
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(os, "name", "nt"),
            patch.dict(sys.modules, {"ctypes": fake_ctypes}),
        ):
            holder = harness_sync._try_omp_lock("C:\\Users\\test\\mcp.json.lock")
            self.assertIsNotNone(holder)
            assert holder is not None
            harness_sync._release_omp_lock(holder)

        self.assertEqual(cleared, [0])
        self.assertEqual(closed, [123])

    def test_omp_lock_hashes_symlinked_target_lexically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            backing = Path(tmp) / "backing"
            home.mkdir()
            backing.mkdir()
            (home / ".omp").symlink_to(backing, target_is_directory=True)
            target = home / ".omp" / "agent" / "mcp.json"
            holder = ("socket", SimpleNamespace(close=lambda: None))
            with patch.object(
                harness_sync, "_try_omp_lock", return_value=holder
            ) as acquire:
                with harness_sync._omp_config_lock(target, retries=1):
                    pass

            acquire.assert_called_once_with(os.path.abspath(target) + ".lock")

    def test_omp_lock_name_matches_upstream_seeded_xxhash(self) -> None:
        self.assertEqual(
            harness_sync._omp_memory_lock_name("/tmp/test.lock"),
            "omp-file-lock-c0b11536af4af31f2f1b828aaf87ec00",
        )

    def test_omp_lock_contends_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mcp.json"
            with harness_sync._omp_config_lock(target, retries=1):
                with self.assertRaises(TimeoutError):
                    with harness_sync._omp_config_lock(target, retries=1):
                        self.fail("contended lock should not be acquired")
            with harness_sync._omp_config_lock(target, retries=1):
                pass

    def test_unknown_windows_reparse_point_is_never_disposable(self) -> None:
        metadata = SimpleNamespace(
            st_file_attributes=harness_sync._FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=0x9000001A,
            st_mode=stat.S_IFDIR,
        )
        with (
            patch.object(os, "lstat", return_value=metadata),
            patch.object(Path, "is_symlink", return_value=False),
        ):
            self.assertTrue(harness_sync._is_reparse_point(Path("cloud-entry")))
            self.assertFalse(harness_sync._is_foreign_link(Path("cloud-entry")))

    def test_symlink_adoption_refuses_unknown_windows_reparse_point(self) -> None:
        with (
            patch.object(harness_sync, "_is_foreign_link", return_value=False),
            patch.object(harness_sync, "_is_reparse_point", return_value=True),
            patch.object(Path, "unlink") as unlink,
            patch.object(harness_sync.shutil, "rmtree") as rmtree,
        ):
            action, detail = harness_sync._safe_symlink(
                Path("source"), Path("cloud-entry"), False, adopt_matching=True
            )

        self.assertEqual(action, "error")
        self.assertIn("unknown reparse point", detail)
        unlink.assert_not_called()
        rmtree.assert_not_called()

    def test_command_wrapper_prune_preserves_linked_external_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "commands"
            target = root / "skills"
            external = root / "external"
            source.mkdir()
            target.mkdir()
            external.mkdir()
            external_wrapper = external / "SKILL.md"
            external_wrapper.write_text("KEEP\n", encoding="utf-8")
            (target / "cmd-stale").symlink_to(external, target_is_directory=True)

            report = harness_sync.Report()
            harness_sync.strategy_command_to_cursor_skill(
                source, target, report, "cursor", False
            )

            self.assertEqual(external_wrapper.read_text(encoding="utf-8"), "KEEP\n")
            self.assertTrue((target / "cmd-stale").is_symlink())
            self.assertEqual(report.by_action(), {"skip": 1})

    def test_cursor_hook_projection_refuses_linked_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "settings.json"
            external = root / "external.json"
            target = root / "cursor" / "hooks.json"
            source.write_text('{"hooks": {}}\n', encoding="utf-8")
            external.write_text('{"external": true}\n', encoding="utf-8")
            target.parent.mkdir()
            target.symlink_to(external)

            report = harness_sync.Report()
            harness_sync.strategy_translate_bootstrap_hook_to_cursor_json(
                source, target, report, "cursor", False
            )

            self.assertEqual(
                external.read_text(encoding="utf-8"), '{"external": true}\n'
            )
            self.assertTrue(target.is_symlink())
            self.assertEqual(report.by_action(), {"error": 1})

    def test_lx_symlink_reparse_point_is_the_only_disposable_tag(self) -> None:
        metadata = SimpleNamespace(
            st_file_attributes=harness_sync._FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=harness_sync._IO_REPARSE_TAG_LX_SYMLINK,
            st_mode=stat.S_IFLNK,
        )
        with (
            patch.object(os, "lstat", return_value=metadata),
            patch.object(Path, "is_symlink", return_value=False),
        ):
            self.assertTrue(harness_sync._is_foreign_link(Path("lx-link")))

    def test_child_projection_refuses_linked_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            external = root / "external"
            target = root / "target"
            source.mkdir()
            external.mkdir()
            (source / "shared").mkdir()
            (external / "keep.txt").write_text("keep\n", encoding="utf-8")
            target.symlink_to(external, target_is_directory=True)

            report = harness_sync.Report()
            harness_sync.strategy_symlink_children(
                source, target, report, "ohmypi", False
            )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertEqual(
                (external / "keep.txt").read_text(encoding="utf-8"), "keep\n"
            )
            self.assertFalse((external / "shared").exists())

    def test_child_projection_preserves_unowned_child_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            external = root / "external"
            source.mkdir()
            target.mkdir()
            external.mkdir()
            (source / "shared").mkdir()
            unowned = target / "unowned"
            unowned.symlink_to(external, target_is_directory=True)

            report = harness_sync.Report()
            harness_sync.strategy_symlink_children(
                source, target, report, "ohmypi", False
            )

            self.assertTrue(unowned.is_symlink())
            self.assertTrue(
                harness_sync._link_points_to(os.readlink(unowned), external)
            )
            self.assertTrue((target / "shared").is_symlink())


class McpProjectionTests(unittest.TestCase):
    def _isolated_claude(self, root: Path):
        return (
            patch.object(
                harness_sync, "CLAUDE_CONFIG_JSON_PATH", root / ".claude.json"
            ),
            patch.object(
                harness_sync,
                "_claude_mcp_managed_path",
                return_value=root / ".claude" / ".harness-sync-managed-mcp.json",
            ),
        )

    def test_claude_manifest_mirror_uses_canonical_override(self) -> None:
        override = Path("/tmp/canonical-mcp.json")
        with patch.object(harness_sync, "MCP_MANIFEST_PATH", override):
            claude = next(
                spec
                for spec in harness_sync._harness_specs()
                if spec["name"] == "claude"
            )

        mirror = next(
            artifact
            for artifact in claude["artifacts"]
            if artifact.get("target_rel") == "mcp-servers.json"
        )
        projection = next(
            artifact
            for artifact in claude["artifacts"]
            if artifact.get("capability") == "mcp"
        )
        self.assertEqual(mirror["source"], override)
        self.assertEqual(projection["source"], override)

    def test_repo_manifest_is_default_even_when_claude_has_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            claude = root / ".claude"
            config.mkdir()
            claude.mkdir()
            (claude / "mcp-servers.json").write_text("{}", encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(harness_sync, "CONFIG_HOME", config),
                patch.object(harness_sync, "CLAUDE_HOME", claude),
            ):
                self.assertEqual(
                    harness_sync._mcp_manifest_path(), config / "mcp-servers.json"
                )

    def test_claude_sidecar_path_is_target_scoped(self) -> None:
        home = Path("/tmp/home")
        self.assertEqual(
            harness_sync._claude_mcp_managed_path(home / ".claude.json"),
            home / ".claude.json.harness-sync-managed-mcp.json",
        )
        self.assertEqual(
            harness_sync._claude_mcp_managed_path(home / "profiles" / "work.json"),
            home / "profiles" / ".work.json.harness-sync-managed-mcp.json",
        )

    def test_portable_secret_references_use_codex_native_indirection(self) -> None:
        block = harness_sync._emit_codex_mcp_block(
            {
                "stdio-shared": {
                    "command": "shared",
                    "env": {"SAME": "${SAME}", "PUBLIC": "literal"},
                },
                "remote-shared": {
                    "type": "http",
                    "url": "https://example.test/mcp",
                    "headers": {
                        "Authorization": "Bearer ${MCP_TOKEN}",
                        "X-API-Key": "${MCP_API_KEY}",
                        "X-Public": "literal",
                    },
                },
            }
        )
        parsed = tomllib.loads(block)["mcp_servers"]

        self.assertEqual(parsed["stdio-shared"]["env"], {"PUBLIC": "literal"})
        self.assertEqual(parsed["stdio-shared"]["env_vars"], ["SAME"])
        self.assertNotIn("type", parsed["remote-shared"])
        self.assertEqual(parsed["remote-shared"]["bearer_token_env_var"], "MCP_TOKEN")
        self.assertEqual(
            parsed["remote-shared"]["env_http_headers"],
            {"X-API-Key": "MCP_API_KEY"},
        )
        self.assertEqual(
            parsed["remote-shared"]["http_headers"], {"X-Public": "literal"}
        )

    def test_codex_rejects_env_alias_and_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "use the same name"):
            harness_sync._emit_codex_mcp_block(
                {"shared": {"command": "shared", "env": {"TARGET": "${SOURCE}"}}}
            )
        with self.assertRaisesRegex(ValueError, "default"):
            harness_sync._emit_codex_mcp_block(
                {"shared": {"command": "shared", "env": {"TOKEN": "${TOKEN:-x}"}}}
            )

    def test_codex_mcp_refuses_linked_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".codex" / "config.toml"
            external = root / "external.toml"
            target.parent.mkdir()
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            external.write_text("# KEEP\n", encoding="utf-8")
            target.symlink_to(external)

            report = harness_sync.Report()
            harness_sync.strategy_mcp_to_codex(manifest, target, report, "codex", False)

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertEqual(external.read_text(encoding="utf-8"), "# KEEP\n")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not portable")
    def test_codex_mcp_backup_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".codex" / "config.toml"
            target.parent.mkdir()
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            target.write_text('secret_header = "sentinel"\n', encoding="utf-8")
            target.chmod(0o600)

            report = harness_sync.Report()
            harness_sync.strategy_mcp_to_codex(manifest, target, report, "codex", False)

            backup = target.parent / ".parity-backups" / "config.toml.pre-mcp-sync"
            self.assertEqual(report.by_action(), {"sync_mcp": 1})
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
            self.assertIn("sentinel", backup.read_text(encoding="utf-8"))

    def test_cursor_projection_translates_portable_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".cursor" / "mcp.json"
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "stdio": {
                                "command": "shared",
                                "env": {"TOKEN": "${TOKEN}"},
                            },
                            "remote": {
                                "type": "http",
                                "url": "https://example.test/mcp",
                                "headers": {"Authorization": "Bearer ${HTTP_TOKEN}"},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_cursor(
                    manifest, target, report, "cursor", False
                )

            config = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
            self.assertEqual(config["stdio"]["env"]["TOKEN"], "${env:TOKEN}")
            self.assertEqual(
                config["remote"]["headers"]["Authorization"],
                "Bearer ${env:HTTP_TOKEN}",
            )
            self.assertEqual(report.by_action(), {"sync_mcp": 1})

    def test_gemini_projection_preserves_native_state_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".gemini" / "settings.json"
            target.parent.mkdir()
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "shared": {
                                "command": "shared",
                                "env": {"MCP_TOKEN": "${MCP_TOKEN}"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            target.write_text(
                json.dumps(
                    {
                        "theme": "dark",
                        "mcpServers": {"local": {"command": "local"}},
                    }
                ),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                first = harness_sync.Report()
                harness_sync.strategy_mcp_to_gemini(
                    manifest, target, first, "gemini", False
                )
                second = harness_sync.Report()
                harness_sync.strategy_mcp_to_gemini(
                    manifest, target, second, "gemini", False
                )

            written = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(written["theme"], "dark")
            self.assertEqual(written["mcpServers"]["local"], {"command": "local"})
            self.assertEqual(
                written["mcpServers"]["shared"]["env"]["MCP_TOKEN"],
                "${MCP_TOKEN}",
            )
            self.assertEqual(first.by_action(), {"sync_mcp": 1})
            self.assertEqual(second.by_action(), {"skip": 1})

    def test_manifest_credentials_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "header": {
                                "type": "http",
                                "url": "https://example.test/mcp",
                                "headers": {"Authorization": "secret-value"},
                            },
                            "url": {
                                "type": "http",
                                "url": "https://example.test/mcp?api_key=secret-value",
                            },
                            "args": {
                                "command": "server",
                                "args": ["--api-key", "secret-value"],
                            },
                            "camel": {
                                "command": "server",
                                "env": {"accessToken": "secret-value"},
                            },
                            "camel-top": {
                                "command": "server",
                                "secretAccessKey": "secret-value",
                            },
                            "nested": {
                                "command": "server",
                                "transport": {"privateKey": "secret-value"},
                            },
                            "segmented": {
                                "command": "server",
                                "env": {
                                    "PRIVATE_KEY_PASSPHRASE": "secret-value",
                                    "SESSION_COOKIE": "secret-value",
                                    "AUTH_VALUE": "secret-value",
                                    "privateKeyPassphrase": "secret-value",
                                    "sessionCookie": "secret-value",
                                    "authValue": "secret-value",
                                },
                                "headers": {
                                    "X-Auth": "secret-value",
                                    "xAuth": "secret-value",
                                },
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertFalse(target.exists())
            detail = report.errors()[0].detail
            self.assertIn("header.headers.Authorization", detail)
            self.assertIn("url.url", detail)
            self.assertIn("args.args", detail)
            self.assertIn("camel.env.accessToken", detail)
            self.assertIn("camel-top.secretAccessKey", detail)
            self.assertIn("nested.transport.privateKey", detail)
            self.assertIn("segmented.env.PRIVATE_KEY_PASSPHRASE", detail)
            self.assertIn("segmented.env.SESSION_COOKIE", detail)
            self.assertIn("segmented.env.AUTH_VALUE", detail)
            self.assertIn("segmented.env.privateKeyPassphrase", detail)
            self.assertIn("segmented.env.sessionCookie", detail)
            self.assertIn("segmented.env.authValue", detail)
            self.assertIn("segmented.headers.X-Auth", detail)
            self.assertIn("segmented.headers.xAuth", detail)

    def test_literal_env_and_header_values_fail_closed_for_unknown_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "unknown-header": {
                                "type": "http",
                                "url": "https://example.test/mcp",
                                "headers": {
                                    "x-functions-key": "0123456789abcdef0123456789abcdef"
                                },
                            },
                            "unknown-env": {
                                "command": "server",
                                "env": {
                                    "SUPABASE_SERVICE_ROLE_KEY": "0123456789abcdef0123456789abcdef"
                                },
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertFalse(target.exists())
            detail = report.errors()[0].detail
            self.assertIn("unknown-header.headers.x-functions-key", detail)
            self.assertIn("unknown-env.env.SUPABASE_SERVICE_ROLE_KEY", detail)

    def test_signed_url_query_credentials_fail_closed(self) -> None:
        for query_key in ("sig", "SAS", "X-Amz-Signature", "X-Goog-Signature"):
            with (
                self.subTest(query_key=query_key),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                manifest = root / "mcp-servers.json"
                target = root / ".omp" / "agent" / "mcp.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                "signed": {
                                    "type": "http",
                                    "url": f"https://example.test/mcp?{query_key}=opaque",
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                registry_patch, sidecar_patch = self._isolated_claude(root)
                with registry_patch, sidecar_patch:
                    report = harness_sync.Report()
                    harness_sync.strategy_mcp_to_omp(
                        manifest, target, report, "ohmypi", False
                    )

                self.assertEqual(report.by_action(), {"error": 1})
                self.assertFalse(target.exists())

    def test_advertised_and_uri_secret_shapes_fail_closed(self) -> None:
        values = {
            "openai-project": "sk-proj-" + "A" * 40,
            "anthropic": "sk-ant-api03-" + "A" * 40,
            "github-fine-grained": "github_pat_" + "A" * 40,
            "aws-temporary": "ASIA" + "A" * 16,
            "database-uri": "".join(
                (
                    "post",
                    "gresql",
                    "://",
                    "fixture-user",
                    ":",
                    "fixture-pass",
                    "@example.test/app",
                )
            ),
            "password-only-uri": "redis://:p4ssw0rd@example.test/0",
            "token-userinfo-uri": "https://opaque-token@example.test/mcp",
            "pem-private-key": "-----BEGIN PRIVATE KEY-----\nopaque\n-----END PRIVATE KEY-----",
        }
        for name, secret in values.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = root / "mcp-servers.json"
                target = root / ".omp" / "agent" / "mcp.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                "opaque": {
                                    "command": "server",
                                    "env": {"VALUE": secret},
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                registry_patch, sidecar_patch = self._isolated_claude(root)
                with registry_patch, sidecar_patch:
                    report = harness_sync.Report()
                    harness_sync.strategy_mcp_to_omp(
                        manifest, target, report, "ohmypi", False
                    )

                self.assertEqual(report.by_action(), {"error": 1})
                self.assertFalse(target.exists())
                self.assertNotIn(secret, report.errors()[0].detail)

    def test_manifest_references_fail_closed_outside_portable_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "alias": {
                                "command": "server",
                                "env": {"TARGET": "${SOURCE}"},
                            },
                            "default": {
                                "command": "server",
                                "env": {"PATH_VALUE": "${PATH_VALUE:-fallback}"},
                            },
                            "args": {"command": "server", "args": ["${TOKEN}"]},
                            "url": {
                                "type": "http",
                                "url": "https://example.test/${TOKEN}",
                            },
                            "bearer-alias": {
                                "type": "http",
                                "url": "https://example.test/mcp",
                                "headers": {"X-Token": "Bearer ${TOKEN}"},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertFalse(target.exists())
            detail = report.errors()[0].detail
            self.assertIn("alias.env.TARGET", detail)
            self.assertIn("default.env.PATH_VALUE", detail)
            self.assertIn("args.args", detail)
            self.assertIn("url.url", detail)
            self.assertIn("bearer-alias.headers.X-Token", detail)

    def test_invalid_portable_server_shapes_fail_before_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            before = '{"mcpServers":{"local":{"command":"local"}}}\n'
            target.write_text(before, encoding="utf-8")
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "bogus": {
                                "type": "bogus",
                                "url": "https://example.test/mcp",
                            },
                            "missing-http": {"type": "http"},
                            "missing-stdio": {"type": "stdio"},
                            "bad-env": {
                                "command": "server",
                                "env": {"VALUE": ["not", "a", "string"]},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertEqual(target.read_text(encoding="utf-8"), before)
            detail = report.errors()[0].detail
            self.assertIn("bogus.type", detail)
            self.assertIn("missing-http.url", detail)
            self.assertIn("missing-stdio.command", detail)
            self.assertIn("bad-env.env", detail)

    def test_secret_shape_scan_blocks_without_disclosing_or_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            sidecar = target.parent / ".harness-sync-managed-mcp.json"
            target.parent.mkdir(parents=True)
            secret = "sk-" + "A" * 20

            # codeql[py/clear-text-storage-sensitive-data]
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "opaque": {
                                "command": "server",
                                "env": {"VALUE": secret},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            target_before = '{"mcpServers":{"local":{"command":"local"}}}\n'
            sidecar_before = '["previously-managed"]\n'
            target.write_text(target_before, encoding="utf-8")
            sidecar.write_text(sidecar_before, encoding="utf-8")
            registry_patch, claude_sidecar_patch = self._isolated_claude(root)
            with registry_patch, claude_sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertEqual(target.read_text(encoding="utf-8"), target_before)
            self.assertEqual(sidecar.read_text(encoding="utf-8"), sidecar_before)
            self.assertNotIn(secret, report.errors()[0].detail)

    def test_secret_shaped_manifest_server_name_is_not_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            secret_name = "sk-" + "A" * 20
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            secret_name: {
                                "command": "server",
                                "env": {"TOKEN": "literal-secret"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            before = '{"mcpServers":{"local":{"command":"local"}}}\n'
            target.write_text(before, encoding="utf-8")
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertEqual(target.read_text(encoding="utf-8"), before)
            self.assertNotIn(secret_name, report.errors()[0].detail)
            self.assertFalse(
                (target.parent / ".harness-sync-managed-mcp.json").exists()
            )

    def test_claude_local_connectors_are_not_cross_harness_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            secret_name = "sk-" + "A" * 20
            manifest.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            (root / ".claude.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            secret_name: {"command": "local-only"},
                            "portable": {"command": "portable"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            servers = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
            self.assertEqual(servers, {})
            self.assertEqual(report.by_action(), {"sync_mcp": 1})
            self.assertIn("manifest=0", report.changes[0].detail)

    def test_secret_claude_connector_stays_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            manifest.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            (root / ".claude.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "secret": {
                                "type": "http",
                                "url": "https://example.test/mcp",
                                "headers": {"Authorization": "secret-value"},
                            },
                            "portable": {"command": "portable"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )
                second = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, second, "ohmypi", False
                )

            servers = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
            self.assertEqual(servers, {})
            self.assertEqual(report.by_action(), {"sync_mcp": 1})
            self.assertIn("manifest=0", report.changes[0].detail)
            self.assertEqual(second.by_action(), {"skip": 1})
            self.assertIn("manifest=0", second.changes[0].detail)

    def test_nonportable_claude_connector_does_not_block_canonical_codex_sync(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".codex" / "config.toml"
            target.parent.mkdir()
            target.write_text("# local\n", encoding="utf-8")
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared-command"}}}),
                encoding="utf-8",
            )
            (root / ".claude.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "local-alias": {
                                "command": "local",
                                "env": {"TARGET": "${SOURCE}"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_codex(
                    manifest, target, report, "codex", False
                )

            parsed = tomllib.loads(target.read_text(encoding="utf-8"))
            self.assertIn("shared", parsed["mcp_servers"])
            self.assertNotIn("local-alias", parsed["mcp_servers"])
            self.assertEqual(report.by_action(), {"sync_mcp": 1})
            self.assertIn("manifest=1", report.changes[0].detail)

    def test_claude_ownership_is_scoped_to_each_registry_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            first_registry = root / "profile-a" / "claude.json"
            second_registry = root / "profile-b" / "claude.json"
            first_registry.parent.mkdir()
            second_registry.parent.mkdir()
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "managed"}}}),
                encoding="utf-8",
            )
            first = harness_sync.Report()
            harness_sync.strategy_mcp_to_claude(
                manifest, first_registry, first, "claude", False
            )

            second_registry.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "profile-b-owned"}}}),
                encoding="utf-8",
            )
            manifest.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            second = harness_sync.Report()
            harness_sync.strategy_mcp_to_claude(
                manifest, second_registry, second, "claude", False
            )

            self.assertEqual(first.by_action(), {"sync_mcp": 1})
            self.assertEqual(second.by_action(), {"sync_mcp": 1})
            self.assertEqual(
                json.loads(second_registry.read_text(encoding="utf-8"))["mcpServers"],
                {"shared": {"command": "profile-b-owned"}},
            )
            self.assertNotEqual(
                harness_sync._claude_mcp_managed_path(first_registry),
                harness_sync._claude_mcp_managed_path(second_registry),
            )

    def test_claude_registry_tracks_manifest_entries_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            registry = root / ".claude.json"
            sidecar = root / ".claude" / ".harness-sync-managed-mcp.json"
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "shared": {
                                "command": "shared-command",
                                "env": {"MCP_TOKEN": "${MCP_TOKEN}"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry.write_text(
                json.dumps({"mcpServers": {"local": {"command": "local-command"}}}),
                encoding="utf-8",
            )
            with patch.object(
                harness_sync, "_claude_mcp_managed_path", return_value=sidecar
            ):
                first = harness_sync.Report()
                harness_sync.strategy_mcp_to_claude(
                    manifest, registry, first, "claude", False
                )
                after_first = json.loads(registry.read_text(encoding="utf-8"))[
                    "mcpServers"
                ]
                owned_after_first = json.loads(sidecar.read_text(encoding="utf-8"))
                manifest.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
                second = harness_sync.Report()
                harness_sync.strategy_mcp_to_claude(
                    manifest, registry, second, "claude", False
                )
                third = harness_sync.Report()
                harness_sync.strategy_mcp_to_claude(
                    manifest, registry, third, "claude", False
                )

            self.assertEqual(set(after_first), {"local", "shared"})
            self.assertEqual(after_first["shared"]["env"]["MCP_TOKEN"], "${MCP_TOKEN}")
            self.assertEqual(owned_after_first, ["shared"])
            self.assertEqual(
                json.loads(registry.read_text(encoding="utf-8"))["mcpServers"],
                {"local": {"command": "local-command"}},
            )
            self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8")), [])
            self.assertEqual(first.by_action(), {"sync_mcp": 1})
            self.assertEqual(second.by_action(), {"sync_mcp": 1})
            self.assertEqual(third.by_action(), {"skip": 1})

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not portable")
    def test_new_claude_registry_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            registry = root / ".claude.json"
            sidecar = root / ".claude" / ".harness-sync-managed-mcp.json"
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )

            with patch.object(
                harness_sync, "_claude_mcp_managed_path", return_value=sidecar
            ):
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_claude(
                    manifest, registry, report, "claude", False
                )

            self.assertEqual(stat.S_IMODE(registry.stat().st_mode), 0o600)
            self.assertEqual(report.by_action(), {"sync_mcp": 1})

    def test_mcp_sidecar_link_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            protected = root / "protected.json"
            sidecar = target.parent / ".harness-sync-managed-mcp.json"
            target.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            protected.write_text('["do-not-change"]\n', encoding="utf-8")
            sidecar.symlink_to(protected)
            registry_patch, claude_sidecar_patch = self._isolated_claude(root)
            with registry_patch, claude_sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertFalse(target.exists())
            self.assertEqual(
                protected.read_text(encoding="utf-8"), '["do-not-change"]\n'
            )

    def test_malformed_mcp_sidecar_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            sidecar = target.parent / ".harness-sync-managed-mcp.json"
            target.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            before = '{"mcpServers":{"local":{"command":"local"}}}\n'
            target.write_text(before, encoding="utf-8")
            sidecar.write_text("not-json\n", encoding="utf-8")
            registry_patch, claude_sidecar_patch = self._isolated_claude(root)
            with registry_patch, claude_sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_mcp_target_link_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            protected = root / "protected.json"
            target.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            before = '{"mcpServers":{"local":{"command":"local"}}}\n'
            protected.write_text(before, encoding="utf-8")
            target.symlink_to(protected)
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertEqual(protected.read_text(encoding="utf-8"), before)

    def test_omp_projection_preserves_native_state_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            agent_home = root / ".omp" / "agent"
            target = agent_home / "mcp.json"
            auth_store = agent_home / "agent.db"
            agent_home.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "shared": {
                                "type": "http",
                                "url": "https://example.test/mcp",
                                "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            target.write_text(
                json.dumps(
                    {
                        "disabledServers": ["paused"],
                        "mcpServers": {
                            "local": {"command": "local"},
                            "shared": {
                                "type": "http",
                                "url": "https://example.test/mcp",
                                "auth": {
                                    "type": "oauth",
                                    "credentialId": "profile-binding",
                                },
                                "oauth": {
                                    "clientId": "public-client",
                                    "callbackPort": 3000,
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            auth_store.write_bytes(b"opaque native oauth state")
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                first = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, first, "ohmypi", False
                )
                second = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, second, "ohmypi", False
                )

            written = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(written["disabledServers"], ["paused"])
            self.assertEqual(set(written["mcpServers"]), {"local", "shared"})
            self.assertEqual(written["mcpServers"]["local"], {"command": "local"})
            self.assertEqual(
                written["mcpServers"]["shared"]["headers"]["Authorization"],
                "Bearer ${MCP_TOKEN}",
            )
            self.assertEqual(
                written["mcpServers"]["shared"]["auth"],
                {"type": "oauth", "credentialId": "profile-binding"},
            )
            self.assertEqual(
                written["mcpServers"]["shared"]["oauth"],
                {"clientId": "public-client", "callbackPort": 3000},
            )
            self.assertEqual(auth_store.read_bytes(), b"opaque native oauth state")
            self.assertEqual(first.by_action(), {"sync_mcp": 1})
            self.assertEqual(second.by_action(), {"skip": 1})

    def test_omp_endpoint_change_with_local_auth_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "shared": {
                                "type": "http",
                                "url": "https://new.example.test/mcp",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            before = (
                json.dumps(
                    {
                        "mcpServers": {
                            "shared": {
                                "type": "http",
                                "url": "https://old.example.test/mcp",
                                "auth": {
                                    "type": "oauth",
                                    "credentialId": "profile-binding",
                                },
                            }
                        }
                    },
                    indent=2,
                )
                + "\n"
            )
            target.write_text(before, encoding="utf-8")
            sidecar = target.parent / ".harness-sync-managed-mcp.json"
            sidecar_before = '["sentinel"]\n'
            sidecar.write_text(sidecar_before, encoding="utf-8")
            agent_db = target.parent / "agent.db"
            agent_db.write_bytes(b"opaque native state")
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertEqual(target.read_text(encoding="utf-8"), before)
            self.assertEqual(sidecar.read_text(encoding="utf-8"), sidecar_before)
            self.assertEqual(agent_db.read_bytes(), b"opaque native state")

    def test_omp_waits_for_native_writer_and_preserves_its_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            contended = threading.Event()
            finished = threading.Event()
            result: list[harness_sync.Report] = []

            def run_sync() -> None:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )
                result.append(report)
                finished.set()

            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                real_try_lock = harness_sync._try_omp_lock

                def observed_try_lock(lock_path: str):
                    acquired = real_try_lock(lock_path)
                    if acquired is None:
                        contended.set()
                    return acquired

                with harness_sync._omp_config_lock(target):
                    with patch.object(
                        harness_sync,
                        "_try_omp_lock",
                        side_effect=observed_try_lock,
                    ):
                        worker = threading.Thread(target=run_sync)
                        worker.start()
                        self.assertTrue(contended.wait(timeout=1))
                        target.write_text(
                            json.dumps(
                                {"mcpServers": {"native": {"command": "native"}}}
                            ),
                            encoding="utf-8",
                        )
                        self.assertFalse(finished.is_set())
                worker.join(timeout=2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0].by_action(), {"sync_mcp": 1})
            written = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
            self.assertEqual(set(written), {"native", "shared"})

    def test_omp_target_publish_failure_recovers_original_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            before = '{"mcpServers":{"local":{"command":"local"}}}\n'
            target.write_text(before, encoding="utf-8")
            real_replace = os.replace

            def fail_target_replace(source: str | Path, destination: str | Path):
                if Path(destination) == target:
                    raise OSError("simulated target failure")
                return real_replace(source, destination)

            registry_patch, sidecar_patch = self._isolated_claude(root)
            with (
                registry_patch,
                sidecar_patch,
                patch.object(os, "replace", side_effect=fail_target_replace),
            ):
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertEqual(target.read_text(encoding="utf-8"), before)
            self.assertEqual(list(target.parent.glob(".mcp.json.*.tmp")), [])
            transaction = harness_sync._mcp_transaction_path(
                target.parent / ".harness-sync-managed-mcp.json"
            )
            self.assertTrue(transaction.is_file())

            recovery = harness_sync.Report()
            self.assertTrue(
                harness_sync._recover_mcp_transaction(
                    target,
                    target.parent / ".harness-sync-managed-mcp.json",
                    recovery,
                    "ohmypi",
                    False,
                )
            )
            self.assertEqual(target.read_text(encoding="utf-8"), before)
            self.assertFalse(
                (target.parent / ".harness-sync-managed-mcp.json").exists()
            )
            self.assertFalse(transaction.exists())
            self.assertEqual(recovery.by_action(), {"sync_mcp": 1})
            self.assertIn("rolled back", recovery.changes[0].detail)

            with registry_patch, sidecar_patch:
                applied = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, applied, "ohmypi", False
                )
                converged = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, converged, "ohmypi", False
                )

            self.assertEqual(applied.by_action(), {"sync_mcp": 1})
            self.assertEqual(converged.by_action(), {"skip": 1})

    def test_omp_sidecar_publish_failure_completes_on_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            sidecar = target.parent / ".harness-sync-managed-mcp.json"
            target.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            target.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "old": {"command": "old"},
                            "local": {"command": "local"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            sidecar.write_text('["old"]\n', encoding="utf-8")
            real_replace = os.replace
            failed = False

            def fail_sidecar_once(source: str | Path, destination: str | Path):
                nonlocal failed
                if Path(destination) == sidecar and not failed:
                    failed = True
                    raise OSError("simulated sidecar failure")
                return real_replace(source, destination)

            registry_patch, claude_sidecar_patch = self._isolated_claude(root)
            with (
                registry_patch,
                claude_sidecar_patch,
                patch.object(os, "replace", side_effect=fail_sidecar_once),
            ):
                interrupted = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, interrupted, "ohmypi", False
                )

            transaction = harness_sync._mcp_transaction_path(sidecar)
            self.assertEqual(interrupted.by_action(), {"error": 1})
            self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8")), ["old"])
            self.assertTrue(transaction.is_file())

            with registry_patch, claude_sidecar_patch:
                recovered = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, recovered, "ohmypi", False
                )
                converged = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, converged, "ohmypi", False
                )

            servers = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
            self.assertEqual(set(servers), {"local", "shared"})
            self.assertEqual(
                json.loads(sidecar.read_text(encoding="utf-8")), ["shared"]
            )
            self.assertFalse(transaction.exists())
            self.assertEqual(recovered.by_action(), {"sync_mcp": 1, "skip": 1})
            self.assertEqual(converged.by_action(), {"skip": 1})

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not portable")
    def test_new_omp_mcp_file_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", False
                )

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(report.by_action(), {"sync_mcp": 1})

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not portable")
    def test_existing_omp_mcp_file_is_tightened_to_native_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            target.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            target.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}, indent=2)
                + "\n",
                encoding="utf-8",
            )
            target.chmod(0o644)
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                first = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, first, "ohmypi", False
                )
                second = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, second, "ohmypi", False
                )

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(first.by_action(), {"sync_mcp": 1})
            self.assertEqual(second.by_action(), {"skip": 1})

    def test_omp_dry_run_creates_no_agent_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".omp" / "agent" / "mcp.json"
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            registry_patch, sidecar_patch = self._isolated_claude(root)
            with registry_patch, sidecar_patch:
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_omp(
                    manifest, target, report, "ohmypi", True
                )

            self.assertEqual(report.by_action(), {"sync_mcp": 1})
            self.assertFalse(target.parent.exists())

    def test_cursor_projection_fails_closed_on_concurrent_native_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "mcp-servers.json"
            target = root / ".cursor" / "mcp.json"
            target.parent.mkdir()
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            target.write_text(
                json.dumps({"mcpServers": {}, "nativeState": "before"}),
                encoding="utf-8",
            )
            concurrent = {"mcpServers": {}, "nativeState": "KEEP"}
            real_atomic = harness_sync._atomic_write_if_changed
            injected = False

            def inject_update(
                path: Path,
                content: str,
                dry_run: bool,
                create_mode: int = 0o644,
                enforce_mode: bool = False,
            ) -> bool:
                nonlocal injected
                if path == target and dry_run and not injected:
                    target.write_text(json.dumps(concurrent), encoding="utf-8")
                    injected = True
                return real_atomic(
                    path,
                    content,
                    dry_run,
                    create_mode=create_mode,
                    enforce_mode=enforce_mode,
                )

            registry_patch, sidecar_patch = self._isolated_claude(root)
            with (
                registry_patch,
                sidecar_patch,
                patch.object(
                    harness_sync, "_atomic_write_if_changed", side_effect=inject_update
                ),
            ):
                report = harness_sync.Report()
                harness_sync.strategy_mcp_to_cursor(
                    manifest, target, report, "cursor", False
                )

            self.assertEqual(report.by_action(), {"error": 1})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), concurrent)
            self.assertFalse(
                (target.parent / ".harness-sync-managed-mcp.json.transaction").exists()
            )

    def test_omp_spec_uses_native_surfaces(self) -> None:
        spec = next(
            item for item in harness_sync._harness_specs() if item["name"] == "ohmypi"
        )
        strategies = [artifact["strategy"] for artifact in spec["artifacts"]]
        mcp = next(
            artifact
            for artifact in spec["artifacts"]
            if artifact["strategy"] is harness_sync.strategy_mcp_to_omp
        )

        self.assertEqual(spec["home"], harness_sync.OMP_AGENT_HOME)
        with tempfile.TemporaryDirectory() as tmp:
            agent_home = Path(tmp)
            (agent_home / "agent.db").write_bytes(b"native OMP state")
            with patch.object(harness_sync, "OMP_PROFILE_VALID", True):
                self.assertTrue(spec["detect"](agent_home))
            with patch.object(harness_sync, "OMP_PROFILE_VALID", False):
                self.assertFalse(spec["detect"](agent_home))
        self.assertEqual(strategies.count(harness_sync.strategy_symlink), 1)
        self.assertEqual(strategies.count(harness_sync.strategy_symlink_children), 3)
        self.assertEqual(mcp["target_rel"], "mcp.json")
        self.assertEqual(mcp["capability"], "mcp")
        self.assertCountEqual(
            [
                (
                    artifact["source"].name,
                    artifact["target_rel"],
                    artifact["strategy"].__name__,
                )
                for artifact in spec["artifacts"]
            ],
            [
                ("AGENTS.md", "AGENTS.md", "strategy_symlink"),
                ("agents", "agents", "strategy_symlink_children"),
                ("commands", "commands", "strategy_symlink_children"),
                ("skills", "skills", "strategy_symlink_children"),
                ("mcp-servers.json", "mcp.json", "strategy_mcp_to_omp"),
            ],
        )

    def test_pi_spec_uses_only_native_core_surfaces(self) -> None:
        spec = next(
            item for item in harness_sync._harness_specs() if item["name"] == "pi"
        )
        self.assertEqual(spec["home"], harness_sync.PI_AGENT_HOME)
        self.assertEqual(
            spec["unsupported_capabilities"]["mcp"],
            "Pi core has no native MCP; manage a reviewed Pi extension separately",
        )
        self.assertCountEqual(
            [
                (
                    artifact["source"].name,
                    artifact["target_rel"],
                    artifact["strategy"].__name__,
                )
                for artifact in spec["artifacts"]
            ],
            [
                ("AGENTS.md", "AGENTS.md", "strategy_symlink"),
                ("commands", "prompts", "strategy_symlink_children"),
                ("skills", "skills", "strategy_symlink_children"),
            ],
        )

    def test_cursor_spec_covers_desktop_and_agent_cli_surfaces(self) -> None:
        spec = next(
            item for item in harness_sync._harness_specs() if item["name"] == "cursor"
        )
        self.assertCountEqual(
            [
                (
                    artifact["source"].name,
                    artifact["target_rel"],
                    artifact["strategy"].__name__,
                )
                for artifact in spec["artifacts"]
            ],
            [
                ("agents", "agents", "strategy_symlink_children"),
                ("skills", "skills", "strategy_symlink_children"),
                ("commands", "skills", "strategy_command_to_cursor_skill"),
                ("hooks", "hooks", "strategy_symlink_children"),
                (
                    "settings.json",
                    "hooks.json",
                    "strategy_translate_bootstrap_hook_to_cursor_json",
                ),
                ("mcp-servers.json", "mcp.json", "strategy_mcp_to_cursor"),
            ],
        )

    def test_cursor_and_pi_detectors_partition_native_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_cli = root / "missing-cli-config.json"
            with patch.object(harness_sync, "CURSOR_CLI_CONFIG_PATH", missing_cli):
                cursor = next(
                    item
                    for item in harness_sync._harness_specs()
                    if item["name"] == "cursor"
                )
                for marker in ("argv.json", "skills-cursor"):
                    with self.subTest(harness="cursor", marker=marker):
                        home = root / f"cursor-{marker.replace('.', '-')}"
                        home.mkdir()
                        path = home / marker
                        if marker.endswith(".json"):
                            path.write_text("{}\n", encoding="utf-8")
                        else:
                            path.mkdir()
                        self.assertTrue(cursor["detect"](home))
                projected = root / "cursor-projected-only"
                (projected / "agents").mkdir(parents=True)
                (projected / "skills").mkdir()
                self.assertFalse(cursor["detect"](projected))
                self.assertFalse(cursor["detect"](root / "cursor-missing"))

            cli_config = root / "cursor-cli" / "cli-config.json"
            cli_config.parent.mkdir()
            cli_config.write_text("{}\n", encoding="utf-8")
            with patch.object(harness_sync, "CURSOR_CLI_CONFIG_PATH", cli_config):
                cursor = next(
                    item
                    for item in harness_sync._harness_specs()
                    if item["name"] == "cursor"
                )
                self.assertTrue(cursor["detect"](root / "cursor-cli-only"))

            pi = next(
                item for item in harness_sync._harness_specs() if item["name"] == "pi"
            )
            for marker in ("settings.json", "auth.json", "sessions"):
                with self.subTest(harness="pi", marker=marker):
                    home = root / f"pi-{marker.replace('.', '-')}"
                    home.mkdir()
                    path = home / marker
                    if marker.endswith(".json"):
                        path.write_text("{}\n", encoding="utf-8")
                    else:
                        path.mkdir()
                    self.assertTrue(pi["detect"](home))
            projected = root / "pi-projected-only"
            (projected / "prompts").mkdir(parents=True)
            (projected / "skills").mkdir()
            (projected / "AGENTS.md").write_text("# projected\n", encoding="utf-8")
            self.assertFalse(pi["detect"](projected))
            self.assertFalse(pi["detect"](root / "pi-missing"))

    def test_cursor_cli_only_sync_targets_shared_home_and_preserves_cli_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            cursor_home = root / ".cursor"
            cli_config = root / "cursor-cli" / "cli-config.json"
            (config / "agents").mkdir(parents=True)
            (config / "commands").mkdir()
            (config / "skills" / "hello").mkdir(parents=True)
            (config / "hooks").mkdir()
            cli_config.parent.mkdir()
            (config / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
            (config / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
            (config / "agents" / "hello.md").write_text("# Agent\n", encoding="utf-8")
            (config / "commands" / "hello.md").write_text(
                "---\ndescription: Hello\n---\nSay hello.\n", encoding="utf-8"
            )
            (config / "skills" / "hello" / "SKILL.md").write_text(
                "---\nname: hello\ndescription: Hello\n---\n# Hello\n",
                encoding="utf-8",
            )
            (config / "hooks" / "bootstrap-agent-config.sh").write_text(
                "#!/bin/sh\n", encoding="utf-8"
            )
            (config / "settings.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {
                                    "hooks": [
                                        {
                                            "command": "$HOME/.claude/hooks/bootstrap-agent-config.sh",
                                            "timeout": 5000,
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            manifest = config / "mcp-servers.json"
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            cli_before = b'{"model":"auto"}\n'
            cli_config.write_bytes(cli_before)

            with (
                patch.object(harness_sync, "CONFIG_HOME", config),
                patch.object(harness_sync, "CURSOR_HOME", cursor_home),
                patch.object(harness_sync, "CURSOR_CLI_CONFIG_PATH", cli_config),
                patch.object(harness_sync, "MCP_MANIFEST_PATH", manifest),
                patch.object(
                    harness_sync, "CLAUDE_CONFIG_JSON_PATH", root / ".claude.json"
                ),
            ):
                dry = harness_sync.sync(only={"cursor"}, dry_run=True)
                self.assertFalse(cursor_home.exists())
                self.assertEqual(cli_config.read_bytes(), cli_before)
                first = harness_sync.sync(only={"cursor"})
                second = harness_sync.sync(only={"cursor"})

            self.assertEqual(dry.by_action(), first.by_action())
            self.assertEqual(
                first.by_action(), {"symlink": 3, "translate": 2, "sync_mcp": 1}
            )
            self.assertEqual(second.by_action(), {"skip": 6})
            self.assertTrue((cursor_home / "agents" / "hello.md").is_symlink())
            self.assertTrue((cursor_home / "skills" / "hello").is_symlink())
            self.assertTrue(
                (cursor_home / "skills" / "cmd-hello" / "SKILL.md").is_file()
            )
            self.assertTrue(
                (cursor_home / "hooks" / "bootstrap-agent-config.sh").is_symlink()
            )
            self.assertEqual(
                json.loads((cursor_home / "hooks.json").read_text(encoding="utf-8"))[
                    "hooks"
                ]["sessionStart"],
                [{"command": "hooks/bootstrap-agent-config.sh", "timeout": 5}],
            )
            self.assertEqual(
                json.loads((cursor_home / "mcp.json").read_text(encoding="utf-8"))[
                    "mcpServers"
                ],
                {"shared": {"command": "shared"}},
            )
            self.assertEqual(cli_config.read_bytes(), cli_before)

    def test_pi_projection_and_explicit_mcp_skip_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            pi_home = root / ".pi" / "agent"
            (config / "commands").mkdir(parents=True)
            (config / "skills" / "hello").mkdir(parents=True)
            (config / "hooks").mkdir()
            pi_home.mkdir(parents=True)
            (config / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
            (config / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
            (config / "commands" / "hello.md").write_text(
                "---\ndescription: Hello\n---\nSay hello.\n", encoding="utf-8"
            )
            (config / "skills" / "hello" / "SKILL.md").write_text(
                "---\nname: hello\ndescription: Hello\n---\n# Hello\n",
                encoding="utf-8",
            )
            (pi_home / "settings.json").write_text("{}\n", encoding="utf-8")
            auth = pi_home / "auth.json"
            auth.write_bytes(b"opaque native auth")

            with (
                patch.object(harness_sync, "CONFIG_HOME", config),
                patch.object(harness_sync, "PI_AGENT_HOME", pi_home),
            ):
                first = harness_sync.sync(only={"pi"})
                second = harness_sync.sync(only={"pi"})
                mcp_only = harness_sync.sync(only={"pi"}, only_capability="mcp")

            self.assertTrue((pi_home / "AGENTS.md").is_symlink())
            self.assertTrue((pi_home / "prompts" / "hello.md").is_symlink())
            self.assertTrue((pi_home / "skills" / "hello").is_symlink())
            self.assertFalse((pi_home / "mcp.json").exists())
            self.assertEqual(auth.read_bytes(), b"opaque native auth")
            self.assertEqual(first.by_action(), {"symlink": 3})
            self.assertEqual(second.by_action(), {"skip": 3})
            self.assertEqual(mcp_only.by_action(), {"skip": 1})
            self.assertIn("no native MCP", mcp_only.changes[0].detail)

    def test_every_mcp_projection_is_capability_tagged(self) -> None:
        expected = {
            "claude": harness_sync.strategy_mcp_to_claude,
            "codex": harness_sync.strategy_mcp_to_codex,
            "cursor": harness_sync.strategy_mcp_to_cursor,
            "gemini": harness_sync.strategy_mcp_to_gemini,
            "ohmypi": harness_sync.strategy_mcp_to_omp,
        }
        specs = {spec["name"]: spec for spec in harness_sync._harness_specs()}
        for harness, strategy in expected.items():
            matches = [
                artifact
                for artifact in specs[harness]["artifacts"]
                if artifact["strategy"] is strategy
            ]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].get("capability"), "mcp")

        tagged = []
        for harness, spec in specs.items():
            for artifact in spec["artifacts"]:
                if artifact.get("capability") != "mcp":
                    continue
                target_name = artifact.get("target_rel")
                if target_name is None:
                    target_name = Path(artifact["target"]).name
                tagged.append((harness, artifact["strategy"].__name__, target_name))
        self.assertCountEqual(
            tagged,
            [
                ("claude", "strategy_mcp_to_claude", ".claude.json"),
                ("codex", "strategy_mcp_to_codex", "config.toml"),
                ("cursor", "strategy_mcp_to_cursor", "mcp.json"),
                ("gemini", "strategy_mcp_to_gemini", "settings.json"),
                ("ohmypi", "strategy_mcp_to_omp", "mcp.json"),
            ],
        )

    def test_mcp_capability_filter_runs_only_mcp_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills").mkdir()
            (root / "hooks").mkdir()
            (root / "CLAUDE.md").write_text("# Test\n", encoding="utf-8")
            harness_home = root / "harness"
            harness_home.mkdir()
            calls: list[str] = []

            def record(*, marker: str, **_kwargs: object) -> None:
                calls.append(marker)

            specs = [
                {
                    "name": "test",
                    "home": harness_home,
                    "role": "translate",
                    "detect": lambda _home: True,
                    "artifacts": [
                        {
                            "strategy": record,
                            "source": root / "CLAUDE.md",
                            "target_rel": "all.md",
                            "opts": {"marker": "all"},
                        },
                        {
                            "strategy": record,
                            "source": root / "mcp-servers.json",
                            "target_rel": "mcp.json",
                            "capability": "mcp",
                            "opts": {"marker": "mcp"},
                        },
                    ],
                }
            ]
            with (
                patch.object(harness_sync, "CONFIG_HOME", root),
                patch.object(harness_sync, "_harness_specs", return_value=specs),
            ):
                harness_sync.sync(only_capability="mcp")

            self.assertEqual(calls, ["mcp"])

    def test_real_mcp_only_dry_run_has_no_filesystem_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            codex = root / ".codex"
            claude = root / ".claude"
            (config / "skills").mkdir(parents=True)
            (config / "hooks").mkdir()
            (config / "CLAUDE.md").write_text("# Test\n", encoding="utf-8")
            manifest = config / "mcp-servers.json"
            manifest.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "shared"}}}),
                encoding="utf-8",
            )
            codex.mkdir()
            config_before = b"# sentinel\r\n"
            (codex / "config.toml").write_bytes(config_before)
            claude.mkdir()
            before = sorted(path.relative_to(root) for path in root.rglob("*"))

            with (
                patch.object(Path, "home", return_value=root),
                patch.object(harness_sync, "CONFIG_HOME", config),
                patch.object(harness_sync, "CLAUDE_HOME", claude),
                patch.object(harness_sync, "MCP_MANIFEST_PATH", manifest),
                patch.object(
                    harness_sync, "CLAUDE_CONFIG_JSON_PATH", root / ".claude.json"
                ),
            ):
                report = harness_sync.sync(
                    only={"codex"}, only_capability="mcp", dry_run=True
                )

            after = sorted(path.relative_to(root) for path in root.rglob("*"))
            self.assertFalse(report.errors())
            self.assertEqual(after, before)
            self.assertEqual((codex / "config.toml").read_bytes(), config_before)


if __name__ == "__main__":
    unittest.main()
