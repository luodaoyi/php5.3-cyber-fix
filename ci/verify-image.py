"""Exercise the published runtime's real extension set in disposable containers."""
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from verify import request

IMAGE = sys.argv[1]
PREFIX = "php53-ci-" + uuid.uuid4().hex[:8]
REPORT = Path("artifacts/image")
REPORT.mkdir(parents=True, exist_ok=True)
checks = []
containers = []


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, capture_output=True, **kwargs).stdout


def check(name, condition):
    checks.append({"check": name, "passed": bool(condition)})
    if not condition:
        raise AssertionError(name)
    print("PASS:", name, flush=True)


def start(name, *args):
    containers.append(name)
    return docker("run", "-d", "--name", name, *args)


class HTTPFixture(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"curl-ok")

    def log_message(self, *args):
        pass


def run():
    docker("network", "create", PREFIX)
    with tempfile.TemporaryDirectory(prefix=PREFIX) as temporary:
        work = Path(temporary)
        work.chmod(0o755)
        (work / "fpm.conf").write_text("""[global]
daemonize = no
error_log = /proc/self/fd/2
[test]
user = devilbox
group = devilbox
listen = 0.0.0.0:19000
pm = static
pm.max_children = 1
pm.max_requests = 0
catch_workers_output = yes
php_admin_value[memory_limit] = 8M
php_admin_value[post_max_size] = 16K
""", encoding="utf-8")
        (work / "index.php").write_text("""<?php
if ($_GET['mode'] === 'fatal') { trigger_error('fixture fatal', E_USER_ERROR); }
if ($_GET['mode'] === 'oom') { $x = str_repeat('x', 64 * 1024 * 1024); }
echo json_encode(array('pid'=>getmypid(), 'files'=>count($_FILES),
    'upload'=>isset($_FILES['f']) ? $_FILES['f']['error'] : -1));
""", encoding="utf-8")
        (work / "oom.php").write_text("<?php function done(){echo 'SHUTDOWN_RAN';} "
                                     "register_shutdown_function('done'); "
                                     "$x=str_repeat('x',64*1024*1024);", encoding="utf-8")
        name = PREFIX + "-fpm"
        start(name, "--network", PREFIX, "--memory", "512m", "--pids-limit", "128",
              "--add-host", "host.docker.internal:host-gateway", "-p", "127.0.0.1::19000",
              "-v", str(work) + ":/tests:ro", "--entrypoint", "php-fpm", IMAGE,
              "-F", "-y", "/tests/fpm.conf")
        port = int(docker("port", name, "19000/tcp").decode().strip().rsplit(":", 1)[1])
        address = ("127.0.0.1", port)
        for attempt in range(100):
            try:
                first = request(address, Path("/tests/index.php"))
                break
            except (ConnectionError, EOFError, OSError):
                if attempt == 99:
                    raise
                time.sleep(0.1)

        def data(**kwargs):
            response = request(address, Path("/tests/index.php"), **kwargs)
            return json.loads(response.split(b"\r\n\r\n", 1)[1])

        pids = [json.loads(first.split(b"\r\n\r\n", 1)[1])["pid"]]
        pids.extend(data()["pid"] for _ in range(4))
        check("full runtime never reuses FPM child", len(set(pids)) == 5)
        request(address, Path("/tests/index.php"), mode="fatal")
        check("full runtime survives fatal and early INI diagnostic",
              data(extra={"PHP_VALUE": "date.timezone=Not/A_Valid_Zone"})["pid"] > 0)
        try:
            request(address, Path("/tests/index.php"), mode="oom")
        except (EOFError, ConnectionResetError):
            pass  # The source patch deliberately closes the failed request.
        check("full runtime replaces OOM child", data()["pid"] > 0)
        oom = subprocess.run(["docker", "exec", name, "php", "-d", "memory_limit=8M",
                              "/tests/oom.php"], capture_output=True, timeout=15)
        (REPORT / "oom.log").write_bytes(oom.stdout + oom.stderr)
        check("full extension set follows OOM fail-stop policy",
              oom.returncode == 1 and b"SHUTDOWN_RAN" not in oom.stdout
              and b"memory size" in oom.stdout + oom.stderr)
        body = (b'--fixture\r\nContent-Disposition: form-data; name="f"; filename="a.txt"'
                b'\r\nContent-Type: text/plain\r\n\r\nhello\r\n--fixture--\r\n')
        upload = data(body=body, content_type="multipart/form-data; boundary=fixture")
        check("full runtime accepts normal uploads", upload["files"] == 1 and upload["upload"] == 0)

        def php(code):
            return docker("exec", name, "php", "-r", code).decode()

        php('$r=array("mysql","mysqli","pdo_mysql","redis","mbstring","curl","openssl","xml","json","hash","session");foreach($r as $e){if(!extension_loaded($e)){exit(1);}}')
        php('$s=mb_convert_encoding("验证", "EUC-JP", "UTF-8");if(mb_convert_encoding($s,"UTF-8","EUC-JP")!=="验证"){exit(1);}')
        php('$x=openssl_encrypt("fixture","aes-128-cbc","1234567890123456",0,"1234567890123456");if(openssl_decrypt($x,"aes-128-cbc","1234567890123456",0,"1234567890123456")!=="fixture"){exit(1);}if((string)simplexml_load_string("<root>ok</root>")!=="ok"){exit(2);}')
        php('session_save_path("/tmp");if(!session_start()){exit(1);}$_SESSION["fixture"]="ok";session_write_close();')
        check("required extensions, encoding, OpenSSL, XML and session work", True)
        server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), HTTPFixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            php('$c=curl_init("http://host.docker.internal:%d/");curl_setopt($c,CURLOPT_RETURNTRANSFER,1);curl_setopt($c,CURLOPT_TIMEOUT,5);if(curl_exec($c)!=="curl-ok"){exit(1);}' % server.server_port)
            check("curl HTTP request works", True)
        finally:
            server.shutdown()
            server.server_close()

        start(PREFIX + "-mysql", "--network", PREFIX, "--network-alias", "mysql",
              "-e", "MYSQL_ALLOW_EMPTY_PASSWORD=yes", "-e", "MYSQL_ROOT_HOST=%",
              "-e", "MYSQL_DATABASE=php53_ci", "mysql:5.7")
        for attempt in range(120):
            ready = subprocess.run(["docker", "exec", name, "php", "-r",
                                    'exit(@mysql_connect("mysql","root","")?0:1);'],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        check("MySQL 5.7 is reachable", ready.returncode == 0)
        php('$c=mysql_connect("mysql","root","");mysql_select_db("php53_ci",$c);if(!mysql_query("CREATE TEMPORARY TABLE fixture (id INT NOT NULL)")){exit(1);}mysql_query("INSERT INTO fixture VALUES (53)");$r=mysql_fetch_row(mysql_query("SELECT id FROM fixture"));if($r[0]!="53"){exit(2);}')
        check("legacy mysql extension reads and writes isolated MySQL 5.7", True)
        start(PREFIX + "-redis", "--network", PREFIX, "--network-alias", "redis", "redis:7-alpine")
        for attempt in range(30):
            ready = subprocess.run(["docker", "exec", name, "php", "-r",
                                    '$r=new Redis();exit(@$r->connect("redis",6379,1)?0:1);'],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        check("Redis is reachable", ready.returncode == 0)
        php('$r=new Redis();$r->connect("redis",6379,1);$r->set("php53_fixture","ok");if($r->get("php53_fixture")!=="ok"){exit(1);}$r->delete("php53_fixture");')
        check("Redis extension reads and writes isolated Redis", True)
        (REPORT / "modules.txt").write_text(php('echo implode("\n",get_loaded_extensions());'), encoding="utf-8")
        (REPORT / "worker-pids.json").write_text(json.dumps(pids), encoding="utf-8")


try:
    run()
finally:
    (REPORT / "checks.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    for name in reversed(containers):
        logs = subprocess.run(["docker", "logs", name], capture_output=True)
        (REPORT / (name + ".log")).write_bytes(logs.stdout + logs.stderr)
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL)
    subprocess.run(["docker", "network", "rm", PREFIX], stdout=subprocess.DEVNULL)
