"""A tiny local SMTP sink for development and tests. It delivers nothing.

    python -m contact_form_backend.devsmtp --port 2525

Accepts EHLO/HELO, AUTH PLAIN/LOGIN (any credentials), MAIL, RCPT, DATA,
RSET, NOOP and QUIT, keeps each message in memory and prints its headers.
No STARTTLS: point the backend at it with ``CFB_SMTP_SECURITY=none``.
Binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import socketserver
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class Received:
    mail_from: str
    rcpt_to: List[str]
    data: bytes
    auth: Optional[str] = None

    @property
    def message(self) -> email.message.EmailMessage:
        return email.message_from_bytes(self.data, policy=email.policy.default)


@dataclass
class _State:
    messages: List[Received] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    on_message: Optional[Callable[[Received], None]] = None


class _Handler(socketserver.StreamRequestHandler):
    server: "_Server"

    def _send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode("ascii"))
        self.wfile.flush()

    def _readline(self) -> Optional[bytes]:
        line = self.rfile.readline(65536)
        return line if line else None

    def handle(self) -> None:
        self._send("220 localhost contact-form-backend dev SMTP sink")
        mail_from, rcpts, auth = "", [], None
        while True:
            raw = self._readline()
            if raw is None:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            arg = line[len(verb):].strip()
            if verb == "EHLO":
                self.wfile.write(
                    b"250-localhost\r\n250-AUTH PLAIN LOGIN\r\n250-8BITMIME\r\n250 SIZE 10485760\r\n"
                )
                self.wfile.flush()
            elif verb == "HELO":
                self._send("250 localhost")
            elif verb == "AUTH":
                parts = arg.split()
                mech = parts[0].upper() if parts else ""
                if mech == "PLAIN":
                    if len(parts) < 2:
                        self._send("334 ")
                        self._readline()
                    auth = "PLAIN"
                elif mech == "LOGIN":
                    if len(parts) < 2:
                        self._send("334 VXNlcm5hbWU6")
                        self._readline()
                    self._send("334 UGFzc3dvcmQ6")
                    self._readline()
                    auth = "LOGIN"
                else:
                    self._send("504 unsupported mechanism")
                    continue
                self._send("235 authenticated")
            elif verb == "MAIL":
                mail_from, rcpts = arg.split(":", 1)[-1].strip().split(" ")[0].strip("<>"), []
                self._send("250 OK")
            elif verb == "RCPT":
                rcpts.append(arg.split(":", 1)[-1].strip().split(" ")[0].strip("<>"))
                self._send("250 OK")
            elif verb == "DATA":
                self._send("354 end with <CRLF>.<CRLF>")
                chunks: List[bytes] = []
                while True:
                    part = self._readline()
                    if part is None:
                        return
                    if part in (b".\r\n", b".\n"):
                        break
                    chunks.append(part[1:] if part.startswith(b"..") else part)
                msg = Received(mail_from, list(rcpts), b"".join(chunks), auth)
                state = self.server.state
                with state.lock:
                    state.messages.append(msg)
                if state.on_message is not None:
                    state.on_message(msg)
                self._send("250 OK queued")
            elif verb == "RSET":
                mail_from, rcpts = "", []
                self._send("250 OK")
            elif verb == "NOOP":
                self._send("250 OK")
            elif verb == "QUIT":
                self._send("221 bye")
                return
            else:
                self._send("502 command not implemented")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr, state: _State) -> None:
        self.state = state
        super().__init__(addr, _Handler)


class SMTPSink:
    """Run the sink in a background thread: ``with SMTPSink() as sink: sink.port``."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        on_message: Optional[Callable[[Received], None]] = None,
    ) -> None:
        self._state = _State(on_message=on_message)
        self._server = _Server((host, port), self._state)
        self.host, self.port = self._server.server_address[:2]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    @property
    def messages(self) -> List[Received]:
        with self._state.lock:
            return list(self._state.messages)

    def start(self) -> "SMTPSink":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "SMTPSink":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _print_message(msg: Received) -> None:
    parsed = msg.message
    print("--- message from %s to %s" % (msg.mail_from, ", ".join(msg.rcpt_to)), flush=True)
    for name in ("From", "To", "Reply-To", "Subject", "Content-Type"):
        if parsed[name] is not None:
            print("%s: %s" % (name, parsed[name]), flush=True)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Local SMTP sink (prints, never delivers).")
    parser.add_argument("--port", type=int, default=2525)
    args = parser.parse_args(argv)
    sink = SMTPSink(port=args.port, on_message=_print_message)
    print("SMTP sink listening on 127.0.0.1:%d (Ctrl+C to stop)" % sink.port, flush=True)
    try:
        sink._server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sink._server.server_close()


if __name__ == "__main__":
    main()

