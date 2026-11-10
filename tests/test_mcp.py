"""Tests for the COVERA MCP server. Standard library only; no COVER install.

Run:  python -m unittest discover -s tests
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from covera_mcp import guides, server
from covera_mcp import tools as catalog


def abs_dir():
    return tempfile.mkdtemp(prefix="covera-test-")


class CatalogTests(unittest.TestCase):
    def test_names_unique_and_prefixed(self):
        names = [t["name"] for t in catalog.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertTrue(name.startswith("covera_"), name)

    def test_every_recovery_subcommand_present(self):
        expected = {"discover", "start", "collect", "verify", "inspect",
                    "publish-plan", "publish"}
        present = {t["subcommand"] for t in catalog.TOOLS
                   if t.get("entry") == "recovery"}
        self.assertEqual(present, expected)

    def test_entries_known(self):
        for t in catalog.TOOLS:
            self.assertIn(t["entry"],
                          {"measure", "recovery", "build", "doctor", "guide"})

    def test_tool_listing_schema(self):
        for t in catalog.TOOLS:
            listing = catalog.tool_listing(t)
            self.assertEqual(listing["name"], t["name"])
            schema = listing["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("readOnlyHint", listing["annotations"])

    def test_guide_topics_match_guides(self):
        guide = catalog.by_name("covera_get_guide")
        self.assertEqual(set(guide["params"]["topic"]["enum"]),
                         set(guides.TOPICS))

    def test_destructive_tools_marked(self):
        for name in ("covera_coverage_build", "covera_recovery_publish"):
            spec = catalog.by_name(name)
            self.assertTrue(spec["annotations"]["destructiveHint"], name)
        for name in ("covera_coverage_measure", "covera_recovery_inspect",
                     "covera_recovery_discover", "covera_doctor"):
            self.assertTrue(catalog.by_name(name)["annotations"]["readOnlyHint"],
                            name)


class ArgvTests(unittest.TestCase):
    def test_value_flag_repeat(self):
        spec = catalog.by_name("covera_coverage_build")
        argv = catalog.to_argv(spec, {
            "openApiServerUrl": "https://cover.example.com:4009",
            "execute": True,
            "failFast": False,
            "targets": ["app", "lib"],
            "parallel": 4,
        })
        self.assertIn("--open-api-server-url", argv)
        self.assertIn("https://cover.example.com:4009", argv)
        self.assertIn("--execute", argv)
        self.assertNotIn("--fail-fast", argv)       # false flag omitted
        self.assertEqual(argv.count("--target"), 2)
        self.assertEqual(argv[argv.index("--parallel") + 1], "4")

    def test_build_does_not_emit_projectdir_as_cwd_only(self):
        spec = catalog.by_name("covera_coverage_build")
        d = abs_dir()
        argv = catalog.to_argv(spec, {"projectDir": d})
        self.assertIn("--project-root", argv)
        self.assertIn(d, argv)

    def test_recovery_repeat_searchroots(self):
        spec = catalog.by_name("covera_recovery_discover")
        a, b = abs_dir(), abs_dir()
        argv = catalog.to_argv(spec, {"source": "x", "searchRoots": [a, b]})
        self.assertEqual(argv.count("--search-root"), 2)

    def test_timeout_for_build(self):
        spec = catalog.by_name("covera_coverage_build")
        self.assertEqual(catalog.timeout_for(spec, {"execute": True}),
                         catalog.BUILD_EXECUTE_TIMEOUT)
        self.assertEqual(catalog.timeout_for(spec, {}),
                         catalog.BUILD_PLAN_TIMEOUT)


class ValidateTests(unittest.TestCase):
    def v(self, name, args):
        return catalog.validate(catalog.by_name(name), args,
                                os.path.isabs, os.path.isdir)

    def test_one_of_measure(self):
        self.assertIsNotNone(self.v("covera_coverage_measure", {}))
        self.assertIsNotNone(self.v("covera_coverage_measure",
                                    {"moduleName": "a", "moduleId": "b"}))
        self.assertIsNone(self.v("covera_coverage_measure",
                                 {"moduleName": "foo.c"}))

    def test_one_of_build(self):
        d = abs_dir()
        self.assertIsNone(self.v("covera_coverage_build", {"projectDir": d}))
        self.assertIsNotNone(self.v("covera_coverage_build", {}))

    def test_required(self):
        self.assertIsNotNone(self.v("covera_recovery_collect", {"source": "x"}))

    def test_unknown_argument(self):
        self.assertIsNotNone(self.v("covera_coverage_measure",
                                    {"moduleName": "a", "nope": 1}))

    def test_abspath(self):
        self.assertIsNotNone(self.v("covera_recovery_inspect",
                                    {"case": "relative/path"}))
        self.assertIsNone(self.v("covera_recovery_inspect",
                                 {"case": abs_dir()}))

    def test_isdir(self):
        missing = os.path.join(abs_dir(), "does-not-exist")
        self.assertIsNotNone(self.v("covera_coverage_build",
                                    {"projectDir": missing}))

    def test_guide_topic_validates(self):
        # The guide tool's `topic` param has no "kind"; validate must not crash.
        self.assertIsNone(self.v("covera_get_guide", {"topic": "recovery"}))
        self.assertIsNotNone(self.v("covera_get_guide", {"topic": "nope"}))
        self.assertIsNotNone(self.v("covera_get_guide", {}))

    def test_enum(self):
        c, cand = abs_dir(), os.path.join(abs_dir(), "cand")
        bad = self.v("covera_recovery_verify",
                     {"case": c, "candidate": cand, "platform": "mac"})
        self.assertIsNotNone(bad)

    def test_types(self):
        self.assertIsNotNone(self.v("covera_coverage_measure",
                                    {"moduleName": "a", "limit": "ten"}))
        self.assertIsNotNone(self.v("covera_coverage_measure",
                                    {"moduleName": "a", "details": "yes"}))
        self.assertIsNotNone(self.v("covera_coverage_build",
                                    {"projectDir": abs_dir(),
                                     "targets": "notalist"}))


class JsonExtractTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(server._extract_json('{"a": 1}'), {"a": 1})

    def test_last_object_after_noise(self):
        text = '[COVER] starting\n[COVER] step 2\n{"status": "SUCCEEDED"}\n'
        self.assertEqual(server._extract_json(text), {"status": "SUCCEEDED"})

    def test_none_when_absent(self):
        self.assertIsNone(server._extract_json("no json here"))


class InstallEnvTests(unittest.TestCase):
    def test_override_applied_when_set(self):
        out = server.install_env({server.INSTALL_PATH_ENV: "  C:/agent  "})
        self.assertEqual(out[server.CLI_INSTALL_ENV], "C:/agent")

    def test_empty_leaves_existing(self):
        env = {server.INSTALL_PATH_ENV: "", server.CLI_INSTALL_ENV: "keep"}
        out = server.install_env(env)
        self.assertEqual(out[server.CLI_INSTALL_ENV], "keep")


class CredentialTests(unittest.TestCase):
    def _install_with_cache(self, url, key):
        install = abs_dir()
        kit = Path(install) / "agent_kit"
        kit.mkdir(parents=True)
        (kit / "open-api-credentials.json").write_text(
            json.dumps({"openApiServerUrl": url, "accessKey": key}),
            encoding="utf-8")
        return install

    def test_origin_identity(self):
        a = server._origin_identity("https://cover.example.com:4009")
        b = server._origin_identity("https://COVER.example.com:4009/")
        self.assertEqual(a, b)
        self.assertIsNone(server._origin_identity("https://user:pw@h:1"))

    def test_cached_credentials_read(self):
        install = self._install_with_cache("https://c.example.com:4009", "SECRET")
        cached = server._cached_credentials({server.CLI_INSTALL_ENV: install})
        self.assertIsNotNone(cached)
        self.assertEqual(cached["url"], "https://c.example.com:4009")

    def test_resolve_env_wins(self):
        env = {server.COVER_KEY_ENV: "ENVKEY"}
        self.assertEqual(server.resolve_cover_key(env, "https://x"), "ENVKEY")

    def test_resolve_cache_match(self):
        install = self._install_with_cache("https://c.example.com:4009", "SECRET")
        env = {server.CLI_INSTALL_ENV: install}
        self.assertEqual(
            server.resolve_cover_key(env, "https://c.example.com:4009"), "SECRET")

    def test_resolve_cache_mismatch_refused(self):
        install = self._install_with_cache("https://c.example.com:4009", "SECRET")
        env = {server.CLI_INSTALL_ENV: install}
        self.assertIsNone(
            server.resolve_cover_key(env, "https://other.example.com:4009"))

    def test_resolve_none_without_server(self):
        install = self._install_with_cache("https://c.example.com:4009", "SECRET")
        env = {server.CLI_INSTALL_ENV: install}
        self.assertIsNone(server.resolve_cover_key(env, None))


class DoctorTests(unittest.TestCase):
    def test_no_secret_in_output(self):
        install = abs_dir()
        kit = Path(install) / "agent_kit"
        kit.mkdir(parents=True)
        (kit / "open-api-credentials.json").write_text(
            json.dumps({"openApiServerUrl": "https://c:4009",
                        "accessKey": "TOPSECRET"}), encoding="utf-8")
        result = server.doctor_result({server.CLI_INSTALL_ENV: install})
        text = result["content"][0]["text"]
        self.assertNotIn("TOPSECRET", text)
        payload = json.loads(text)
        self.assertTrue(payload["credentials"]["cached"])
        self.assertEqual(payload["credentials"]["serverUrl"], "https://c:4009")

    def test_reports_missing_install(self):
        result = server.doctor_result({})
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("missing", payload)
        self.assertFalse(payload["installPath"]["set"])


class CallToolTests(unittest.TestCase):
    def setUp(self):
        self._orig = server.run_command
        self.calls = []

    def tearDown(self):
        server.run_command = self._orig

    def _fake(self, stdout, *, write_output=None, record_env=None):
        def fake(cmd, cwd=None, timeout=None, on_tick=None, env_extra=None,
                 label=None):
            self.calls.append({"cmd": cmd, "cwd": cwd, "env_extra": env_extra})
            if write_output is not None and "--output" in cmd:
                out = cmd[cmd.index("--output") + 1]
                Path(out).write_text(json.dumps(write_output), encoding="utf-8")
            return (stdout, ""), 0, None
        return fake

    def test_measure_parses_stdout(self):
        if server.entry_path("measure") is None:
            self.skipTest("measure skill not present")
        server.run_command = self._fake('{"status": "SUCCEEDED", "coverage": {}}')
        spec = catalog.by_name("covera_coverage_measure")
        result = server.call_tool(spec, {"moduleName": "foo.c"})
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["status"],
                         "SUCCEEDED")

    def test_measure_failed_is_error(self):
        if server.entry_path("measure") is None:
            self.skipTest("measure skill not present")
        server.run_command = self._fake('{"status": "FAILED", "code": "X"}')
        result = server.call_tool(catalog.by_name("covera_coverage_measure"),
                                  {"moduleId": "0" * 24})
        self.assertTrue(result["isError"])

    def test_build_reads_output_file(self):
        if server.entry_path("build") is None:
            self.skipTest("build skill not present")
        envelope = {"schema": "w", "status": "WAITING_USER", "stages": []}
        server.run_command = self._fake("[COVER] noise", write_output=envelope)
        result = server.call_tool(catalog.by_name("covera_coverage_build"),
                                  {"projectDir": abs_dir()})
        self.assertFalse(result["isError"])  # WAITING_* is not an error
        self.assertEqual(json.loads(result["content"][0]["text"])["status"],
                         "WAITING_USER")

    def test_build_failed_is_error(self):
        if server.entry_path("build") is None:
            self.skipTest("build skill not present")
        server.run_command = self._fake("x", write_output={"status": "FAILED"})
        result = server.call_tool(catalog.by_name("covera_coverage_build"),
                                  {"projectDir": abs_dir()})
        self.assertTrue(result["isError"])

    def test_recovery_subcommand_in_cmd(self):
        if server.entry_path("recovery") is None:
            self.skipTest("recovery skill not present")
        server.run_command = self._fake('{"status": "OK"}')
        server.call_tool(catalog.by_name("covera_recovery_inspect"),
                         {"case": abs_dir()})
        self.assertIn("inspect", self.calls[0]["cmd"])

    def test_recovery_injects_env_key(self):
        if server.entry_path("recovery") is None:
            self.skipTest("recovery skill not present")
        server.run_command = self._fake('{"status": "OK"}')
        os.environ[server.COVER_KEY_ENV] = "ENVKEY"
        try:
            server.call_tool(catalog.by_name("covera_recovery_publish"),
                             {"server": "https://c:4009", "case": abs_dir(),
                              "planDigest": "d"})
        finally:
            del os.environ[server.COVER_KEY_ENV]
        self.assertEqual(self.calls[0]["env_extra"], {server.COVER_KEY_ENV: "ENVKEY"})

    def test_no_json_is_error(self):
        if server.entry_path("measure") is None:
            self.skipTest("measure skill not present")
        server.run_command = self._fake("not json at all")
        result = server.call_tool(catalog.by_name("covera_coverage_measure"),
                                  {"moduleName": "foo.c"})
        self.assertTrue(result["isError"])


class JsonRpcTests(unittest.TestCase):
    def _server(self):
        self.out = []
        lock = threading.Lock()

        def send(msg):
            with lock:
                self.out.append(msg)
        return server.Server(send)

    def _reply(self, rid, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for msg in list(self.out):
                if msg.get("id") == rid and ("result" in msg or "error" in msg):
                    return msg
            time.sleep(0.01)
        self.fail("no reply for id %r" % rid)

    def test_initialize_and_lists(self):
        srv = self._server()
        try:
            srv.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1,
                                        "method": "initialize", "params": {}}))
            init = self._reply(1)
            self.assertEqual(init["result"]["serverInfo"]["name"], "covera-mcp")
            srv.handle_line(json.dumps({"jsonrpc": "2.0", "id": 2,
                                        "method": "tools/list"}))
            tools = self._reply(2)["result"]["tools"]
            self.assertEqual(len(tools), len(catalog.TOOLS))
            srv.handle_line(json.dumps({"jsonrpc": "2.0", "id": 3,
                                        "method": "prompts/list"}))
            prompts = self._reply(3)["result"]["prompts"]
            self.assertEqual({p["name"] for p in prompts},
                             {"coverage-build", "coverage-measure",
                              "toolchain-recovery"})
        finally:
            srv.close()
            srv.join(5)

    def test_unknown_tool(self):
        srv = self._server()
        try:
            srv.handle_line(json.dumps({"jsonrpc": "2.0", "id": 9,
                                        "method": "tools/call",
                                        "params": {"name": "nope"}}))
            self.assertIn("error", self._reply(9))
        finally:
            srv.close()
            srv.join(5)

    def test_validation_error_is_tool_result(self):
        srv = self._server()
        try:
            srv.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": 10, "method": "tools/call",
                "params": {"name": "covera_coverage_measure", "arguments": {}}}))
            reply = self._reply(10)
            self.assertIn("result", reply)
            self.assertTrue(reply["result"]["isError"])
        finally:
            srv.close()
            srv.join(5)

    def test_get_guide_inline(self):
        srv = self._server()
        try:
            srv.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": 12, "method": "tools/call",
                "params": {"name": "covera_get_guide",
                           "arguments": {"topic": "recovery"}}}))
            reply = self._reply(12)
            self.assertIn("result", reply)
            self.assertFalse(reply["result"]["isError"])
        finally:
            srv.close()
            srv.join(5)

    def test_doctor_runs_inline(self):
        srv = self._server()
        try:
            srv.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": 11, "method": "tools/call",
                "params": {"name": "covera_doctor", "arguments": {}}}))
            payload = json.loads(self._reply(11)["result"]["content"][0]["text"])
            self.assertIn("python", payload)
        finally:
            srv.close()
            srv.join(5)


class GuideTests(unittest.TestCase):
    def test_reads_measure_skill(self):
        text, err = guides.read_guide("measure")
        if err and "not installed" in err:
            self.skipTest("skills not present")
        self.assertIsNone(err)
        self.assertIn("covera_get_guide topic=measure", text)
        self.assertNotIn("\n---\nname:", text[:50])  # frontmatter stripped

    def test_unknown_topic(self):
        text, err = guides.read_guide("nope")
        self.assertIsNone(text)
        self.assertIn("unknown topic", err)

    def test_prompt_formats_target(self):
        value, err = guides.get_prompt("coverage-build", {"projectDir": "/p"})
        self.assertIsNone(err)
        self.assertIn("/p", value["messages"][0]["content"]["text"])

    def test_prompt_without_arg_asks(self):
        value, _ = guides.get_prompt("coverage-measure", {})
        self.assertIn("ask", value["messages"][0]["content"]["text"].lower())


class VersionTests(unittest.TestCase):
    def test_versions_agree(self):
        version = server.SERVER_INFO["version"]
        server_json = json.loads((ROOT / "server.json").read_text("utf-8"))
        manifest = json.loads((ROOT / "manifest.json").read_text("utf-8"))
        self.assertEqual(server_json["version"], version)
        self.assertEqual(manifest["version"], version)
        pyproject = (ROOT / "pyproject.toml").read_text("utf-8")
        m = re.search(r'version\s*=\s*"([^"]+)"', pyproject)
        self.assertEqual(m.group(1), version)

    def test_registry_name_and_package(self):
        server_json = json.loads((ROOT / "server.json").read_text("utf-8"))
        self.assertEqual(server_json["name"],
                         "io.github.SuresoftTechnologies/covera-mcp")
        self.assertIn("mcp", server_json["packages"][0]["identifier"])


if __name__ == "__main__":
    unittest.main()
