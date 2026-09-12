#!/usr/bin/env python3
"""A deterministic stand-in for the real ``pi`` binary, for adapter tests.

It speaks the verified Pi 0.74.2 RPC protocol (strict JSON-lines over
stdin/stdout) but never touches a model, so the Odysseus adapter can be tested
without a live local Qwen server.

Scenario control is read from ``<cwd>/.pi_stub.json`` rather than the
environment, because the adapter deliberately launches Pi with a minimal
environment allowlist (that allowlist is itself under test). Recognised keys:

    scenario      "ok" (default) | "tool_fail" | "provider_fail"
                  | "slow" | "instant_fail"
                  | "retry_then_ok" | "retry_exhausted"
    settle        emit Pi 0.85.1's ``agent_settled`` after the terminal agent_end
    linger        keep the process alive after the scenario (as real RPC Pi does)
    attempts      failed-attempt count for the "retry_exhausted" scenario
    session_id    fixed session id to report (default: derived from --session
                  file, else a fresh stub id)
    write_files   list of paths to create in cwd during the run
    run_command   command string reported as a bash tool execution
    exit_code     override the process exit code

It also records, in cwd, ``.pi_stub_args.json`` (argv) and
``.pi_stub_env.json`` (the environment it received).
"""
import json
import os
import sys
import threading
import time


def _load_config(cwd: str) -> dict:
    path = os.path.join(cwd, ".pi_stub.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _parse_args(argv):
    opts = {"session_dir": None, "session": None, "provider": None, "model": None}
    for i, arg in enumerate(argv):
        for key, flag in (("session_dir", "--session-dir"), ("session", "--session"),
                          ("provider", "--provider"), ("model", "--model")):
            if arg == flag and i + 1 < len(argv):
                opts[key] = argv[i + 1]
    return opts


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _session_id_for(opts, cfg, cwd):
    """Resume semantics: a --session file keeps its original session id."""
    if cfg.get("session_id"):
        return cfg["session_id"]
    session_file = opts.get("session")
    if session_file and os.path.isfile(session_file):
        try:
            with open(session_file, "r", encoding="utf-8") as fh:
                first = fh.readline().strip()
            if first:
                head = json.loads(first)
                if head.get("sessionId"):
                    return head["sessionId"]
        except (OSError, ValueError):
            pass
    return "stub-session-" + str(abs(hash(cwd)) % 10**8)


def _ensure_session_file(opts, session_id, cwd):
    session_dir = opts.get("session_dir")
    if not session_dir:
        return None
    os.makedirs(session_dir, exist_ok=True)
    path = os.path.join(session_dir, session_id + ".jsonl")
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "session", "sessionId": session_id,
                                 "cwd": cwd}) + "\n")
    return path


def log_prompt(opts, session_id, message, fallback_dir):
    """Record that a prompt was delivered.

    Written to the (cwd-independent) session dir so a test can prove the task
    was never handed to Pi even when the stub has chdir'd elsewhere.
    """
    directory = opts.get("session_dir") or fallback_dir
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "pi_stub_prompts.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"session_id": session_id, "message": message}) + "\n")
        return path
    except OSError:
        return None
class Stub:
    def __init__(self, opts, cfg, cwd, session_id, session_file):
        self.opts = opts
        self.cfg = cfg
        self.cwd = cwd
        self.initial_cwd = cwd
        self.session_id = session_id
        self.session_file = session_file
        self.busy = False
        self.aborted = False
        #: cwd to record when the session file is created lazily on first prompt.
        self.pending_session_cwd = None

    def _write_files(self):
        for rel in self.cfg.get("write_files") or []:
            target = os.path.join(self.cwd, rel)
            os.makedirs(os.path.dirname(target) or self.cwd, exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("stub output\n")

    def _attempt(self, ok, command=None, write_files=None):
        """Emit one agent attempt (Pi 0.85.1 ends EVERY attempt with agent_end)."""
        _emit({"type": "agent_start"})
        _emit({"type": "turn_start"})
        _emit({"type": "message_start", "message": {"role": "assistant"}})
        if command:
            _emit({"type": "tool_execution_start", "toolName": "bash",
                   "args": {"command": command}})
            _emit({"type": "tool_execution_end", "toolName": "bash", "isError": False})
        for rel in write_files or []:
            _emit({"type": "tool_execution_start", "toolName": "write", "args": {"path": rel}})
            _emit({"type": "tool_execution_end", "toolName": "write", "isError": False})
        if ok and self.cfg.get("tool_error"):
            # A TRANSIENT tool error during an otherwise successful run.
            _emit({"type": "tool_execution_start", "toolName": "bash", "args": {"command": "false"}})
            _emit({"type": "tool_execution_end", "toolName": "bash", "isError": True})
        end_message = {"role": "assistant"}
        if ok:
            _emit({"type": "message_update",
                   "assistantMessageEvent": {"type": "text_delta", "delta": "Working..."}})
            end_message = {
                "role": "assistant",
                "content": [{"type": "text", "text": "Implemented the change and ran tests."}],
            }
            _emit({"type": "turn_end", "message": end_message})
        else:
            # A failed attempt produces NO assistant text. With ``error_stop``,
            # Pi 0.85.1 also attaches stopReason/errorMessage (protocol-native
            # provider/model failure evidence).
            end_message = {"role": "assistant", "content": []}
            if self.cfg.get("error_stop"):
                end_message = {"role": "assistant", "content": [],
                               "stopReason": "error",
                               "errorMessage": str(self.cfg["error_stop"])}
            _emit({"type": "message_end", "message": dict(end_message)})
            _emit({"type": "turn_end", "message": dict(end_message)})
        _emit({"type": "agent_end", "messages": [dict(end_message)]})

    def _settle(self, payload=None):
        """Emit Pi 0.85.1's run-settled event when the scenario asks for it."""
        if self.cfg.get("settle"):
            _emit({"type": "agent_settled", "messages": payload or []})

    def _finish_after_scenario(self):
        """``linger`` mimics a real RPC Pi that stays alive after a run, so the
        adapter must decide completion from events, not from process exit."""
        if self.cfg.get("linger"):
            self.busy = False
            return
        # One-shot usage: exit so the adapter observes process completion.
        os._exit(int(self.cfg.get("exit_code") or 0))

    def run_scenario(self):
        scenario = (self.cfg.get("scenario") or "ok").strip()
        self.busy = True
        try:
            note = self.cfg.get("stderr_note")
            if note:
                # Mimic Pi's merged stderr diagnostics (a non-JSON stdout line).
                sys.stdout.write(str(note) + "\n")
                sys.stdout.flush()
            if scenario == "provider_fail":
                return
            if scenario == "instant_fail":
                os._exit(int(self.cfg.get("exit_code") or 2))
            if scenario == "slow":
                while not self.aborted:
                    time.sleep(0.2)
                return
            if scenario == "tool_fail":
                _emit({"type": "agent_start"})
                _emit({"type": "turn_start"})
                _emit({"type": "tool_execution_start", "toolName": "bash",
                       "args": {"command": "pytest -q"}})
                _emit({"type": "tool_execution_end", "toolName": "bash", "isError": True})
                self.busy = False
                os._exit(int(self.cfg.get("exit_code") or 1))

            if scenario == "retry_then_ok":
                # Attempt 1 fails, a retry fires, attempt 2 succeeds, then settle.
                self._attempt(ok=False)
                _emit({"type": "auto_retry_start", "attempt": 1})
                self._attempt(ok=True)
                self._settle([{"role": "assistant"}])
                self._finish_after_scenario()
                return

            if scenario == "retry_exhausted":
                attempts = int(self.cfg.get("attempts") or 3)
                for i in range(attempts):
                    self._attempt(ok=False)
                    if i < attempts - 1:
                        _emit({"type": "auto_retry_start", "attempt": i + 1})
                _emit({"type": "auto_retry_end", "success": False})
                self._settle([])
                self._finish_after_scenario()
                return

            if scenario == "settle_noop":
                # Invalid-model shape: no assistant output, no tool call, no
                # retry, but Pi settles and exits 0.
                self._attempt(ok=False)
                self._settle([])
                self._finish_after_scenario()
                return

            # default "ok": one successful attempt
            command = self.cfg.get("run_command") or "pytest -q"
            self._attempt(ok=True, command=command,
                          write_files=self.cfg.get("write_files") or [])
            self._write_files()
            self._settle([{"role": "assistant"}])
            self._finish_after_scenario()
        finally:
            self.busy = False

    # -- command dispatch ---------------------------------------------------
    def handle(self, cmd):
        kind = cmd.get("type")
        req_id = cmd.get("id")

        if kind == "get_state":
            _emit({"id": req_id, "type": "response", "command": "get_state", "success": True,
                   "data": {"sessionId": self.session_id,
                            "model": {"provider": self.opts.get("provider"),
                                      "id": self.opts.get("model")},
                            "isStreaming": self.busy}})
            return

        if kind == "get_session_stats":
            _emit({"id": req_id, "type": "response", "command": "get_session_stats",
                   "success": True,
                   "data": {"sessionId": self.session_id, "sessionFile": self.session_file,
                            "userMessages": 1, "toolCalls": 2,
                            "tokens": {"input": 10, "output": 5, "total": 15}}})
            return

        if kind == "set_session_name":
            _emit({"id": req_id, "type": "response", "command": "set_session_name",
                   "success": True})
            return

        if kind == "prompt":
            if self.session_file is None and self.cfg.get("session_file_late"):
                self.session_file = _ensure_session_file(
                    self.opts, self.session_id, self.pending_session_cwd or self.initial_cwd)
            log_prompt(self.opts, self.session_id, cmd.get("message") or "", self.initial_cwd)
            if (self.cfg.get("scenario") or "ok").strip() == "provider_fail":
                _emit({"id": req_id, "type": "response", "command": "prompt",
                       "success": False, "error": "model unavailable (stub)"})
                _emit({"type": "auto_retry_start", "attempt": 1})
                _emit({"type": "auto_retry_end", "success": False})
                self.busy = False
                os._exit(int(self.cfg.get("exit_code") or 3))
            _emit({"id": req_id, "type": "response", "command": "prompt", "success": True})
            threading.Thread(target=self.run_scenario, daemon=True).start()
            return

        if kind == "abort":
            self.aborted = True
            _emit({"id": req_id, "type": "response", "command": "abort", "success": True})
            _emit({"type": "turn_end", "message": {"role": "assistant", "content": []}})
            _emit({"type": "agent_end", "messages": []})
            time.sleep(0.05)
            os._exit(0)

        if kind == "bash":
            _emit({"id": req_id, "type": "response", "command": "bash", "success": True,
                   "data": {"output": "", "exitCode": 0, "cancelled": False,
                            "truncated": False}})
            return

        # Unknown commands are answered (never hang a test).
        _emit({"id": req_id, "type": "response", "command": kind,
               "success": False, "error": "unsupported command (stub)"})


def main() -> int:
    cwd = os.getcwd()
    argv = sys.argv[1:]
    opts = _parse_args(argv)
    cfg = _load_config(cwd)
    try:
        with open(os.path.join(cwd, ".pi_stub_args.json"), "w", encoding="utf-8") as fh:
            json.dump(argv, fh)
        with open(os.path.join(cwd, ".pi_stub_env.json"), "w", encoding="utf-8") as fh:
            json.dump(dict(os.environ), fh, indent=2)
    except OSError:
        pass

    # Simulate a Pi that adopts a remembered/default directory instead of the
    # one it was launched in (the stale-worktree failure mode).
    if cfg.get("chdir_to"):
        try:
            os.chdir(cfg["chdir_to"])
        except OSError:
            pass

    session_id = _session_id_for(opts, cfg, cwd)
    # The session header records the cwd Pi believes it is using: an explicit
    # `session_cwd` simulates a session remembered from a different worktree.
    session_cwd = cfg.get("session_cwd") or os.getcwd()
    # Real Pi may write its session file only once a run starts; `session_file_late`
    # reproduces that so the adapter's late assignment check is exercised.
    session_file = None
    if not cfg.get("session_file_late"):
        session_file = _ensure_session_file(opts, session_id, session_cwd)
    stub = Stub(opts, cfg, cwd, session_id, session_file)
    stub.pending_session_cwd = session_cwd

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        stub.handle(cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())

