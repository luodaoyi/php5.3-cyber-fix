"""Bounded, loopback-only mechanism checks; not an RCE exploit or safety proof."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
MODE = sys.argv[1]
OUT = ROOT / "artifacts" / MODE
PHP = OUT / "php/bin/php"
FPM = OUT / "php/sbin/php-fpm"
PATCHED = MODE != "baseline"
results = []


def check(name, condition):
    results.append({"check": name, "passed": bool(condition)})
    if not condition:
        raise AssertionError(name)
    print("PASS:", name, flush=True)


def record(kind, payload):
    return struct.pack("!BBHHBB", 1, kind, 1, len(payload), 0, 0) + payload


def length(n):
    return bytes([n]) if n < 128 else struct.pack("!I", n | 0x80000000)


def receive(sock, n):
    chunks = bytearray()
    while len(chunks) < n:
        part = sock.recv(n - len(chunks))
        if not part:
            raise EOFError("FPM closed the request")
        chunks.extend(part)
    return bytes(chunks)


def request(address, script, mode="normal", body=b"", content_type="", extra=None):
    params = {
        "SCRIPT_FILENAME": str(script), "SCRIPT_NAME": "/index.php",
        "REQUEST_METHOD": "POST" if body else "GET", "QUERY_STRING": "mode=" + mode,
        "REQUEST_URI": "/index.php?mode=" + mode, "SERVER_PROTOCOL": "HTTP/1.1",
        "SERVER_NAME": "localhost", "SERVER_PORT": "80", "REMOTE_ADDR": "127.0.0.1",
        "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": content_type,
    }
    params.update(extra or {})
    encoded = b""
    for k, v in params.items():
        k, v = k.encode(), v.encode()
        encoded += length(len(k)) + length(len(v)) + k + v
    output = bytearray()
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(10)
        sock.connect(str(address))
        # KEEP_CONN deliberately requested: patched workers must still exit.
        sock.sendall(record(1, struct.pack("!HB5x", 1, 1)))
        sock.sendall(record(4, encoded) + record(4, b""))
        for offset in range(0, len(body), 32000):
            sock.sendall(record(5, body[offset:offset + 32000]))
        sock.sendall(record(5, b""))
        while True:
            _, kind, _, size, padding, _ = struct.unpack("!BBHHBB", receive(sock, 8))
            payload = receive(sock, size)
            receive(sock, padding)
            if kind == 6:
                output.extend(payload)
            if kind == 3:
                return bytes(output)


def run():
    with tempfile.TemporaryDirectory(prefix="php53-") as directory:
        work = Path(directory)
        oom = work / "oom.php"
        oom.write_text("<?php function done() { echo 'SHUTDOWN_RAN'; } "
                       "register_shutdown_function('done'); "
                       "$x = str_repeat('x', 64 * 1024 * 1024);", encoding="utf-8")
        proc = subprocess.run([str(PHP), "-n", "-d", "memory_limit=8M", str(oom)],
                              capture_output=True, timeout=15)
        (OUT / "oom.log").write_bytes(proc.stdout + proc.stderr)
        check("OOM was reached", b"memory size" in proc.stdout + proc.stderr)
        check("OOM skips shutdown only in patched build",
              (b"SHUTDOWN_RAN" not in proc.stdout) == PATCHED)
        check("OOM exits with expected status", proc.returncode == (1 if PATCHED else 255))

        # Force an actual mmap failure, not just PHP's configured memory limit.
        if MODE != "asan":
            shim = work / "fail-mmap.so"
            subprocess.run(["cc", "-shared", "-fPIC", str(ROOT / "ci/fail-mmap.c"),
                            "-o", str(shim)], check=True)
            env = dict(os.environ, LD_PRELOAD=str(shim), ZEND_MM_MEM_TYPE="mmap_anon")
            proc = subprocess.run([str(PHP), "-n", "-d", "memory_limit=-1", str(oom)],
                                  env=env, capture_output=True, timeout=15)
            (OUT / "allocation-failure.log").write_bytes(proc.stdout + proc.stderr)
            check("OS allocation failure was injected", b"INJECTED_MMAP_FAILURE" in proc.stderr)
            check("OS failure follows expected shutdown policy",
                  (b"SHUTDOWN_RAN" not in proc.stdout) == PATCHED)
            check("OS failure exits with expected status", proc.returncode == (1 if PATCHED else 255))

        smoke = """<?php
session_save_path(dirname(__FILE__));
session_start(); $_SESSION['ok'] = 'session-ok'; session_write_close();
$required = array('mysql', 'mysqli', 'pdo_mysql', 'mbstring', 'session', 'filter');
foreach ($required as $ext) { if (!extension_loaded($ext)) { exit(2); } }
if (mb_strlen('abc', 'UTF-8') !== 3) { exit(3); }
echo 'COMPAT_OK';
"""
        compat = work / "compat.php"
        compat.write_text(smoke, encoding="utf-8")
        proc = subprocess.run([str(PHP), "-n", str(compat)], capture_output=True, timeout=10)
        check("legacy extensions and basic session work", proc.returncode == 0 and proc.stdout == b"COMPAT_OK")

        script = work / "index.php"
        script.write_text("""<?php
if ($_GET['mode'] === 'fatal') { trigger_error('fixture fatal', E_USER_ERROR); }
if ($_GET['mode'] === 'oom') { $x = str_repeat('x', 64 * 1024 * 1024); }
header('Content-Type: application/json');
echo json_encode(array('pid' => getmypid(), 'post' => count($_POST),
    'files' => count($_FILES), 'upload' => isset($_FILES['f']) ? $_FILES['f']['error'] : -1));
""", encoding="utf-8")
        address = work / "fpm.sock"
        config = work / "fpm.conf"
        config.write_text(f"""[global]
daemonize = no
error_log = {OUT}/fpm-error.log
[test]
listen = {address}
pm = static
pm.max_children = 1
pm.max_requests = 0
catch_workers_output = yes
php_admin_value[memory_limit] = 8M
php_admin_value[post_max_size] = 16K
php_admin_value[max_input_nesting_level] = 8
""", encoding="utf-8")
        with (OUT / "fpm-console.log").open("wb") as log:
            master = subprocess.Popen([str(FPM), "-n", "-y", str(config)], stdout=log, stderr=log)
            try:
                for _ in range(200):
                    if address.exists() or master.poll() is not None:
                        break
                    time.sleep(0.05)
                check("FPM starts", address.exists() and master.poll() is None)

                def data(**kwargs):
                    response = request(address, script, **kwargs)
                    return json.loads(response.split(b"\r\n\r\n", 1)[1])

                pids = [data()["pid"] for _ in range(5)]
                check("worker reuse matches build policy",
                      len(set(pids)) == (5 if PATCHED else 1))
                request(address, script, mode="fatal")
                after = data(extra={"PHP_VALUE": "date.timezone=Not/A_Valid_Zone"})
                check("request after fatal and early INI diagnostic succeeds", after["pid"] > 0)
                try:
                    request(address, script, mode="oom")
                except (EOFError, ConnectionResetError):
                    if not PATCHED:
                        raise
                check("master replaces OOM worker", data()["pid"] > 0 and master.poll() is None)
                boundary = "php53fixture"
                body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"f\"; filename=\"a.txt\""
                        f"\r\nContent-Type: text/plain\r\n\r\nhello\r\n--{boundary}--\r\n").encode()
                upload = data(body=body, content_type="multipart/form-data; boundary=" + boundary)
                check("normal multipart upload works", upload["files"] == 1 and upload["upload"] == 0)
                oversized = data(body=b"a=" + b"x" * 32000,
                                 content_type="application/x-www-form-urlencoded")
                check("declared oversized POST is rejected", oversized["post"] == 0)
                (OUT / "worker-pids.json").write_text(json.dumps(pids), encoding="utf-8")
            finally:
                master.terminate()
                try:
                    master.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    master.kill()
                    master.wait()


try:
    run()
finally:
    (OUT / "checks.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
