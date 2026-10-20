"""Integration tests use synthetic secrets and isolated temporary state only."""

import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor


AL = Path(os.environ.get("ALOG_TEST_EXECUTABLE", Path(__file__).resolve().parents[1] / "bin/al"))
PYTHON = "/usr/bin/python3"


class AlogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alog-test-")
        self.root = Path(self.temp.name)
        self.logs = self.root / "logs"
        # Deliberately don't inherit real credentials into tests.
        self.env = {
            "HOME": str(self.root),
            "PATH": "/usr/bin:/bin",
            "ALOG_DIR": str(self.logs),
            "LANG": "C.UTF-8",
        }

    def tearDown(self):
        self.temp.cleanup()

    def run_al(self, *args, input=None, env=None):
        return subprocess.run(
            [PYTHON, str(AL), *args], cwd=self.root, env=env or self.env,
            input=input, text=True, capture_output=True, timeout=10,
        )

    def command(self, source, *options, env=None, input=None):
        return self.run_al(*options, "--", PYTHON, "-c", source, env=env, input=input)

    def metadata(self):
        return [json.loads(path.read_text()) for path in self.logs.glob("*/*/meta.json")]

    def test_default_mode_stdout_stderr_exit_and_handoff(self):
        result = self.run_al("/bin/sh", "-c", "printf 'hello\\n'; printf 'oops\\n' >&2; exit 7")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, "hello\noops\n")
        log = next(self.logs.glob("*/*/output.log"))
        self.assertIn(f"Tell agent: read {log}", result.stderr)
        self.assertEqual(log.read_text(), result.stdout)
        meta = self.metadata()[0]
        self.assertEqual(meta["exit_code"], 7)
        self.assertEqual(meta["status"], "complete")
        self.assertNotIn("command", meta)
        self.assertEqual(self.run_al("--read").stdout, result.stdout)
        self.assertEqual(self.run_al("--path").stdout.strip(), str(log))
        self.assertIn(meta["id"], self.run_al("--list").stdout)

    def test_version_does_not_create_logs(self):
        result = self.run_al("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^alog \d+\.\d+\.\d+\n$")
        self.assertFalse(self.logs.exists())

    def test_no_shell_evaluation_and_no_arguments_saved(self):
        marker = "argument-only-test-marker"
        result = self.run_al("/bin/true", marker, "$(touch not-created)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "not-created").exists())
        for path in self.logs.rglob("*"):
            if path.is_file():
                self.assertNotIn(marker, path.read_text())

    def test_missing_command_records_failure(self):
        result = self.run_al("alog-test-nonexistent")
        self.assertEqual(result.returncode, 127)
        self.assertEqual(self.metadata()[0]["exit_code"], 127)
        self.assertIn("could not start command", self.run_al("--read").stdout)

    def test_private_modes_and_no_raw_spool(self):
        self.command("print('hello')")
        self.assertEqual(self.logs.stat().st_mode & 0o777, 0o700)
        for path in self.logs.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
            if path.is_file():
                self.assertIn(path.name, ("output.log", "meta.json"))

    def test_rejects_shared_or_symlinked_log_root(self):
        self.logs.mkdir(mode=0o755)
        self.assertNotEqual(self.command("print('unexpected')").returncode, 0)
        self.logs.rmdir()
        target = self.root / "target"
        target.mkdir(mode=0o700)
        self.logs.symlink_to(target)
        self.assertNotEqual(self.command("print('unexpected')").returncode, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_rejects_symlinked_log_on_read(self):
        self.command("print('hello')")
        log = next(self.logs.glob("*/*/output.log"))
        log.unlink()
        target = self.root / "secret"
        target.write_text("must-not-read")
        log.symlink_to(target)
        result = self.run_al("--read")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("must-not-read", result.stdout)

    def test_concurrent_runs_are_independent_and_context_scoped(self):
        def run_one(index):
            return self.command(f"print('output-{index}')", "-c", "task-one")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run_one, range(12)))
        self.assertTrue(all(result.returncode == 0 for result in results))
        metas = self.metadata()
        self.assertEqual(len({meta["id"] for meta in metas}), 12)
        self.assertEqual(
            {p.read_text().strip() for p in self.logs.glob("*/*/output.log")},
            {f"output-{i}" for i in range(12)},
        )
        self.command("print('other-context')", "-c", "task-two")
        self.assertNotIn("other-context", self.run_al("-c", "task-one", "--read").stdout)
        latest = max(metas, key=lambda meta: (meta["started_ns"], meta["id"]))
        result = self.run_al("-c", "task-one", "--read", latest["id"][:5])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(latest["id"], result.stderr)

    def test_new_context_and_invalid_context(self):
        a = self.run_al("--new-context", "printer")
        b = self.run_al("--new-context", "printer")
        self.assertRegex(a.stdout.strip(), r"^printer-[0-9a-f]{12}$")
        self.assertNotEqual(a.stdout, b.stdout)
        self.assertFalse(self.logs.exists())
        result = self.run_al("-c", "../escape", "/bin/true")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "escape").exists())

    def test_ambiguous_prefix_fails_instead_of_guessing(self):
        for index in range(2):
            self.command(f"print({index})", "-c", "ambiguous")
        for index, run in enumerate(sorted((self.logs / "ambiguous").iterdir())):
            meta = json.loads((run / "meta.json").read_text())
            meta["id"] = f"abcd{index:06x}"
            (run / "meta.json").write_text(json.dumps(meta))
            run.rename(run.parent / meta["id"])
        result = self.run_al("-c", "ambiguous", "--read", "abcd")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("ambiguous", result.stderr)

    def test_live_read_and_latest_are_ordered_by_start_not_finish(self):
        proc = subprocess.Popen([
            PYTHON, str(AL), "-c", "ordering", PYTHON, "-c",
            "import time; from pathlib import Path\n"
            "print('older-start', flush=True)\n"
            "while not Path('release').exists(): time.sleep(.01)\n"
            "print('older-done')",
        ], cwd=self.root, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline(), "older-start\n")
            live = self.run_al("-c", "ordering", "--read")
            self.assertIn("older-start", live.stdout)
            self.assertIn("status=running", live.stderr)
            self.command("print('newer-start')", "-c", "ordering")
            (self.root / "release").touch()
            proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(self.run_al("-c", "ordering", "--read").stdout, "newer-start\n")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    def test_redaction_across_chunks_and_environment_values(self):
        env = {**self.env, "SERVICE_TOKEN": "synthetic-environment-value", "CUSTOM": "extra-secret-value"}
        source = (
            "import os, time\n"
            "os.write(1, b'pass')\n"
            "time.sleep(.02)\n"
            "os.write(1, b'word=hunter-test-value\\n')\n"
            "print(os.environ['SERVICE_TOKEN'])\n"
            "print(os.environ['CUSTOM'])\n"
            "print('Authorization: Bearer abcdefghijklmnop')\n"
            "print('https://user:synthetic-pass@example.invalid/path')\n"
            "print('ghp_' + 'a' * 36)\n"
            "print('sk-' + 'b' * 30)\n"
            "print('\\x1b[31mAPI_KEY=colored-secret-value\\x1b[0m')\n"
            "print('normal diagnostic')\n"
        )
        result = self.command(source, "--redact-env", "CUSTOM", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("normal diagnostic", result.stdout)
        everything = result.stdout + "".join(
            p.read_text() for p in self.logs.rglob("*") if p.is_file()
        )
        for secret in ("hunter-test-value", "synthetic-environment-value", "extra-secret-value",
                       "abcdefghijklmnop", "synthetic-pass", "a" * 36, "b" * 30, "colored-secret-value"):
            self.assertNotIn(secret, everything)
        self.assertIn("[REDACTED", everything)

    def test_private_key_block_and_multiline_field(self):
        result = self.command(
            "print('before\\n-----BEGIN OPENSSH PRIVATE KEY-----\\n"
            "synthetic-key-body\\n-----END OPENSSH PRIVATE KEY-----\\nafter')"
        )
        self.assertIn("before", result.stdout)
        self.assertIn("after", result.stdout)
        self.assertNotIn("synthetic-key-body", result.stdout)
        result = self.command("print('password:\\n  hidden-test-value\\nafter')")
        self.assertNotIn("hidden-test-value", result.stdout)
        self.assertNotIn("after", result.stdout)
        self.assertIn("remaining output omitted", result.stdout)

    def test_remote_password_prompts_keep_later_diagnostics(self):
        prompts = (
            "[sudo] password for example-user: ",
            "example-user@server.example.invalid's password: ",
            "example-user@2001:db8::1's password: ",
            "New SMB password:",
            "Retype new SMB password:",
        )
        for index, prompt in enumerate(prompts):
            with self.subTest(prompt=prompt):
                context = f"prompt-{index}"
                # Remote SSH output includes prompts and CRLF; split a prompt
                # across writes so it cannot rely on a single output chunk.
                source = (
                    "import os, time\n"
                    f"prompt = {prompt!r}.encode()\n"
                    "os.write(2, b'\\x1b[33m' + prompt[:8])\n"
                    "time.sleep(.02)\n"
                    "os.write(2, prompt[8:] + b'\\x1b[0m\\r\\n')\n"
                    "print('service check passed')\n"
                    "raise SystemExit(7)\n"
                )
                result = self.command(source, "-c", context)
                self.assertEqual(result.returncode, 7, result.stderr)
                expected = "[REDACTED password prompt]\nservice check passed\n"
                self.assertEqual(result.stdout, expected)
                self.assertEqual(self.run_al("-c", context, "--read").stdout, expected)
                meta = next(m for m in self.metadata() if m["context"] == context)
                self.assertFalse(meta["sensitive_tail_omitted"])

    def test_password_prompt_at_eof_is_redacted(self):
        result = self.command("import os; os.write(2, b'[sudo] password for example: ')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "[REDACTED password prompt]\n")
        self.assertFalse(self.metadata()[0]["sensitive_tail_omitted"])

    def test_prompt_like_secret_fields_still_suppress_tail(self):
        fields = (
            "password:",
            "password: |",
            "password = {",
            'password: """',
            '  "[sudo] password for example":',
            "[sudo] password for example: password:",
            "New SMB password: >",
            "metadata: example@server.example.invalid's password:",
            "credentials:\n[ sudo ] password for example:",
        )
        for index, field in enumerate(fields):
            with self.subTest(field=field):
                context = f"field-{index}"
                source = f"print({field!r}); print('synthetic-hidden-value'); print('later')"
                result = self.command(source, "-c", context)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("remaining output omitted", result.stdout)
                self.assertNotIn("synthetic-hidden-value", result.stdout)
                self.assertNotIn("later", result.stdout)
                self.assertEqual(self.run_al("-c", context, "--read").stdout, result.stdout)
                meta = next(m for m in self.metadata() if m["context"] == context)
                self.assertTrue(meta["sensitive_tail_omitted"])

    def test_prompts_do_not_end_secret_suppression(self):
        for index, opener in enumerate(("password:", "-----BEGIN OPENSSH PRIVATE KEY-----")):
            with self.subTest(opener=opener):
                source = (
                    f"print({opener!r})\n"
                    "print('[sudo] password for example: ')\n"
                    "print('synthetic-hidden-value')\n"
                )
                result = self.command(source, "-c", f"suppressed-{index}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("password prompt", result.stdout)
                self.assertNotIn("synthetic-hidden-value", result.stdout)

    def test_echoed_value_after_prompt_is_not_a_prompt_exception(self):
        result = self.command(
            "print('[sudo] password for example: synthetic-echoed-value'); print('later')"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "[REDACTED sensitive line]\nlater\n")
        self.assertNotIn("synthetic-echoed-value", self.run_al("--read").stdout)

    def test_input_not_recorded(self):
        result = self.command("input(); print('received')", input="synthetic-typed-secret\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "received\n")
        self.assertNotIn("synthetic-typed-secret", self.run_al("--read").stdout)

    def test_oversized_unterminated_line_is_never_partially_saved(self):
        result = self.command("print('x' * 70000 + 'hidden-at-end'); print('later')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("hidden-at-end", result.stdout)
        self.assertNotIn("xxxx", result.stdout)
        self.assertNotIn("later", result.stdout)
        self.assertIn("oversized line", result.stdout)

    def test_size_limit_and_quiet_preserve_exit_code(self):
        result = self.command("print('test-line\\n' * 100); raise SystemExit(9)", "--max-bytes", "50", "--quiet")
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stdout, "")
        log = self.run_al("--read").stdout
        self.assertIn("log limit reached", log)
        self.assertLess(len(log.encode()), 150)
        self.assertTrue(self.metadata()[0]["truncated"])

    def test_terminal_prompt_live_and_input_not_recorded(self):
        self.assert_terminal_prompt_capture(
            "Synthetic password: ", "getpass.getpass('Synthetic password: ')",
            "colored\naccepted\n",
        )

    def test_remote_style_terminal_prompt_keeps_output_and_hides_input(self):
        # Like SSH relaying a remote prompt, this writes the prompt to captured
        # stderr while reading the hidden password from the original terminal.
        self.assert_terminal_prompt_capture(
            "[sudo] password for example: ",
            "getpass.getpass('[sudo] password for example: ', stream=sys.stderr)",
            "colored\n[REDACTED password prompt]\naccepted\n",
        )

    def assert_terminal_prompt_capture(self, prompt, getpass_call, expected_log):
        # A real controlling terminal exercises getpass, like sudo, without sudo.
        pid, master = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.execve(PYTHON, [
                PYTHON, str(AL), "/usr/bin/python3", "-c",
                "import getpass, sys; "
                "print('\\x1b[32mcolored\\x1b[0m' if sys.stdout.isatty() else 'no-color', flush=True); "
                f"{getpass_call}; print('accepted')",
            ], self.env)
        output = b""
        deadline = time.monotonic() + 8
        sent = False
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], .1)
                if not ready:
                    continue
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if prompt.encode() in output and not sent:
                    os.write(master, b"synthetic-terminal-password\n")
                    sent = True
            waited, status = os.waitpid(pid, os.WNOHANG)
            if not waited:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                self.fail("terminal capture hung")
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
        finally:
            os.close(master)
        self.assertTrue(sent)
        self.assertIn(b"\x1b[32mcolored\x1b[0m", output)
        self.assertNotIn(b"synthetic-terminal-password", output)
        self.assertEqual(self.run_al("--read").stdout, expected_log)
        self.assertFalse(self.metadata()[0]["sensitive_tail_omitted"])
        for path in self.logs.rglob("*"):
            if path.is_file():
                self.assertNotIn("synthetic-terminal-password", path.read_text())

    def test_signal_is_forwarded_and_log_finalized(self):
        proc = subprocess.Popen([
            PYTHON, str(AL), PYTHON, "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ], cwd=self.root, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            ready = False
            while time.monotonic() < deadline:
                logs = list(self.logs.glob("*/*/output.log"))
                if logs and "ready" in logs[0].read_text():
                    ready = True
                    break
                time.sleep(.02)
            self.assertTrue(ready)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 143, (stdout, stderr))
            self.assertEqual(self.metadata()[0]["exit_code"], 143)
            self.assertEqual(self.metadata()[0]["status"], "complete")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
