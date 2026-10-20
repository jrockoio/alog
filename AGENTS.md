# Working on alog

- Linux, Python 3.10+, standard library only. Keep the single-file executable
  usable without packaging tools or network access.
- Run `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -v`.
  Use synthetic credentials and temporary directories for tests.
- Preserve live terminal color, inherited stdin, command exit codes, private
  permissions, per-run isolation, and redaction before disk writes.
- Never save raw output as a temporary spool or add command arguments,
  environment contents, or keyboard input to metadata.
- Redaction is best effort. Document limitations and avoid guarantees that
  arbitrary output is safe to share.
- Keep the public repository free of personal machine configuration and logs.
- Release assets must come from a committed, allowlisted source state via
  `scripts/build-release.py`. Version tags must match `VERSION` in `bin/al`.
