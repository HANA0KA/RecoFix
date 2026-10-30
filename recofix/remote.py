"""NAS and Linux recovery over SSH (Recoverit "NAS and Linux Recovery").

Recoverit's libdrstoragedevice embeds libssh2 (``SSHConnector``) and reads the
remote disks over the network. RecoFix uses the system OpenSSH client:

1. Discover servers on the LAN (Bonjour ``_ssh._tcp`` / ``_smb._tcp`` / ``_afpovertcp._tcp``).
2. Connect with a password or key, list block devices (``lsblk``).
3. Stream the chosen device or partition to a local image (``dd`` over SSH;
   the remote side only reads) and scan that image like any local disk.

Nothing is written to the NAS. Passwords are handed to ssh through a
one-shot ``SSH_ASKPASS`` helper and never stored.
"""
import json
import os
import shlex
import stat
import subprocess
import tempfile
import threading
import time

from .util import chown_user


def discover(timeout=4.0):
    """[{name, type, host?}] advertised on the local network."""
    found = []
    for svc, label in (("_ssh._tcp", "SSH"), ("_smb._tcp", "SMB"), ("_afpovertcp._tcp", "AFP")):
        try:
            p = subprocess.Popen(["dns-sd", "-B", svc, "local."], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True)
            time.sleep(timeout / 3)
            p.terminate()
            out = p.communicate(timeout=2)[0]
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            parts = line.split(None, 6)
            if len(parts) >= 7 and parts[1] == "Add":
                name = parts[6].strip()
                if not any(f["name"] == name and f["type"] == label for f in found):
                    found.append({"name": name, "type": label, "host": name.replace(" ", "-") + ".local"})
    return found


class SSH:
    def __init__(self, host, user, password=None, port=22, key=None, sudo_password=None):
        self.host, self.user, self.port, self.key = host, user, int(port or 22), key
        self.password = password
        self.sudo_password = sudo_password if sudo_password is not None else password

    def _env_and_args(self, tmpdir):
        env = dict(os.environ)
        args = ["ssh", "-p", str(self.port), "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ServerAliveInterval=15", "-o", "LogLevel=ERROR"]
        if self.key:
            args += ["-i", self.key, "-o", "BatchMode=yes"]
        if self.password and not self.key:
            helper = os.path.join(tmpdir, "askpass.sh")
            with open(helper, "w") as f:
                f.write("#!/bin/sh\nprintf '%s\\n' \"$RECOFIX_SSH_PASS\"\n")
            os.chmod(helper, stat.S_IRWXU)
            env.update(SSH_ASKPASS=helper, SSH_ASKPASS_REQUIRE="force", DISPLAY=env.get("DISPLAY", ":0"),
                       RECOFIX_SSH_PASS=self.password)
            args += ["-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "NumberOfPasswordPrompts=1"]
        args += ["%s@%s" % (self.user, self.host)]
        return env, args

    def run(self, command, timeout=60, sudo=False, stdin=None):
        with tempfile.TemporaryDirectory(prefix="recofix_ssh_") as tmp:
            env, args = self._env_and_args(tmp)
            cmd = command
            data = stdin
            if sudo and self.user != "root":
                cmd = "sudo -S -p '' sh -c %s" % shlex.quote(command)
                data = ((self.sudo_password or "") + "\n").encode() + (stdin or b"")
            p = subprocess.run(args + [cmd], input=data, capture_output=True, timeout=timeout, env=env)
            return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")

    def test(self):
        code, out, err = self.run("uname -sr; id -u", timeout=30)
        if code != 0:
            return {"ok": False, "error": err.strip() or "no se pudo conectar"}
        lines = out.decode("utf-8", "replace").split("\n")
        return {"ok": True, "system": lines[0], "root": lines[1:2] == ["0"]}

    def devices(self):
        code, out, err = self.run("lsblk -J -b -o NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,MODEL 2>/dev/null || "
                                  "cat /proc/partitions", timeout=60)
        if code != 0:
            raise RuntimeError(err.strip() or "no se pudieron listar los discos remotos")
        txt = out.decode("utf-8", "replace")
        try:
            data = json.loads(txt)
            flat = []

            def walk(nodes, depth=0):
                for n in nodes:
                    flat.append({"name": n.get("name"), "path": n.get("path") or "/dev/" + n.get("name", ""),
                                 "size": int(n.get("size") or 0), "type": n.get("type"), "fs": n.get("fstype") or "",
                                 "label": n.get("label") or "", "mount": n.get("mountpoint") or "",
                                 "model": (n.get("model") or "").strip(), "depth": depth})
                    walk(n.get("children") or [], depth + 1)

            walk(data.get("blockdevices", []))
            return flat
        except ValueError:
            out = []
            for line in txt.splitlines()[2:]:
                parts = line.split()
                if len(parts) == 4:
                    out.append({"name": parts[3], "path": "/dev/" + parts[3], "size": int(parts[2]) * 1024,
                                "type": "disk" if not parts[3][-1].isdigit() else "part", "fs": "", "label": "",
                                "mount": "", "model": "", "depth": 0 if not parts[3][-1].isdigit() else 1})
            return out


class RemoteImager:
    """Stream a remote block device into a local raw image (read-only on the remote side)."""

    def __init__(self, ssh, device, out_path, size=0):
        self.ssh, self.device, self.out, self.size = ssh, device, out_path, size
        self.done = 0
        self.error = ""
        self.finished = None
        self.started = time.time()
        self.cancel = threading.Event()
        self.proc = None

    def status(self):
        el = (self.finished or time.time()) - self.started
        sp = self.done / el if el > 0 else 0
        return {"done_bytes": self.done, "total_bytes": self.size, "progress": self.done / self.size if self.size else 0,
                "speed": sp, "eta": (self.size - self.done) / sp if sp and self.size else None, "error": self.error,
                "done": self.finished is not None, "out": self.out, "phase": "copiando por red"}

    def run(self):
        tmp = tempfile.mkdtemp(prefix="recofix_ssh_")
        try:
            env, args = self.ssh._env_and_args(tmp)
            dev = shlex.quote(self.device)
            cmd = "dd if=%s bs=4M conv=noerror,sync status=none" % dev
            data = None
            if self.ssh.user != "root":
                cmd = "sudo -S -p '' %s" % cmd
                data = ((self.ssh.sudo_password or "") + "\n").encode()
            self.proc = subprocess.Popen(args + [cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=env)
            if data:
                self.proc.stdin.write(data)
            self.proc.stdin.close()
            with open(self.out, "xb") as w:
                while not self.cancel.is_set():
                    b = self.proc.stdout.read(4 << 20)
                    if not b:
                        break
                    w.write(b)
                    self.done += len(b)
            if self.cancel.is_set():
                self.proc.kill()
            rc = self.proc.wait(timeout=30)
            if rc != 0 and not self.cancel.is_set():
                self.error = self.proc.stderr.read().decode("utf-8", "replace").strip() or "dd remoto terminó con %d" % rc
            chown_user(self.out)
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            self.finished = time.time()
