#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fve_setpoint.py  –  Nastavenie ziadanej hodnoty cinneho vykonu na FTV
=====================================================================

Samostatny, prenositelny modul (vytiahnuty z appky FVE Trakany).
Prihlasi sa cez SSH na jump host a tam spusti 'modpoll', ktory zapise
ziadanu hodnotu vykonu do Huawei SmartLoggera.

Princip zapisu (Huawei SmartLogger):
    register 40428  "Active power adjustment by percentage", gain 10
    zapisana hodnota = percento * 10   ->  1000 = 100 % (naplno), 0 = vypnute

Pouzitie ako modul:
    from fve_setpoint import FvePowerControl
    fve = FvePowerControl()          # alebo s vlastnymi parametrami, viz nizsie
    ok, msg = fve.connect()
    if ok:
        fve.set_percent(50)          # 50 %  -> zapise 500 do registra 40428
        fve.set_raw(1000)            # priamo hodnota registra (0..1000)
    fve.close()

Pouzitie ako CLI:
    python3 fve_setpoint.py 50       # nastav 50 %
    python3 fve_setpoint.py --raw 1000

Zavislosti: len Python 3 (stdlib) + 'ssh' v PATH + 'modpoll' na vzdialenom serveri.
"""

import os
import subprocess
import sys


class FvePowerControl:
    def __init__(self,
                 ssh_host="10.200.136.21",
                 ssh_user="support",
                 ssh_port=8222,
                 ssh_key="~/.ssh/support.rsa",
                 device_ip="192.168.1.250",   # IP SmartLoggera (z pohladu servera)
                 modpoll="modpoll",           # nazov/cesta modpoll na serveri
                 slave_id=0,                  # modpoll -a0
                 ctrl_register=40428,         # riadiaci register
                 gain=10,                     # zapis = percento * gain
                 control_path="/tmp/fve_setpoint_ssh.sock"):
        self.host = ssh_host
        self.user = ssh_user
        self.port = ssh_port
        self.key = os.path.expanduser(ssh_key)
        self.device_ip = device_ip
        self.modpoll = modpoll
        self.slave_id = slave_id
        self.ctrl_register = ctrl_register
        self.gain = gain
        self.ctrl_path = control_path

    # ----- interne -----
    @property
    def _target(self):
        return f"{self.user}@{self.host}"

    @property
    def _ssh_base(self):
        return ["-i", self.key, "-p", str(self.port),
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=12",
                "-o", f"ControlPath={self.ctrl_path}"]

    def _master_alive(self):
        r = subprocess.run(["ssh"] + self._ssh_base + ["-O", "check", self._target],
                           capture_output=True, text=True)
        return r.returncode == 0

    # ----- verejne API -----
    def connect(self):
        """Otvori zdielane (master) SSH spojenie na pozadi. Vrati (ok, sprava)."""
        if self._master_alive():
            return True, "uz pripojene"
        if os.path.exists(self.ctrl_path):
            try:
                os.remove(self.ctrl_path)
            except OSError:
                pass
        cmd = ["ssh"] + self._ssh_base + ["-o", "ControlMaster=yes",
                                          "-o", "ControlPersist=300",
                                          "-N", "-f", self._target]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        except subprocess.TimeoutExpired:
            return False, "SSH spojenie vyprsalo (timeout)."
        if self._master_alive():
            return True, "pripojene"
        return False, (p.stderr or p.stdout or "neznama chyba").strip()

    def close(self):
        """Zatvori zdielane SSH spojenie."""
        subprocess.run(["ssh"] + self._ssh_base + ["-O", "exit", self._target],
                       capture_output=True, text=True)

    def set_raw(self, raw):
        """Zapise priamo hodnotu registra (0..1000). Vrati (ok, vystup)."""
        raw = int(raw)
        cmd = (f"{self.modpoll} -a{self.slave_id} -r{self.ctrl_register} "
               f"-i -0 -c1 -t4 {self.device_ip} {raw}")
        ssh = ["ssh"] + self._ssh_base + ["-o", "ControlMaster=no", self._target, cmd]
        try:
            p = subprocess.run(ssh, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return False, "TIMEOUT: server/modpoll neodpovedal."
        out = (p.stdout or "") + (p.stderr or "")
        low = out.lower()
        ok = ("written" in low) or (p.returncode == 0 and "exception" not in low
                                    and "error" not in low and "illegal" not in low
                                    and "can't reach" not in low)
        return ok, out.strip()

    def set_percent(self, pct):
        """Nastav vykon v percentach 0..100. Vrati (ok, vystup)."""
        pct = max(0, min(100, int(pct)))
        return self.set_raw(pct * self.gain)


def _cli(argv):
    raw_mode = "--raw" in argv
    args = [a for a in argv if a != "--raw"]
    if len(args) != 1:
        print(__doc__)
        return 2
    try:
        value = int(args[0])
    except ValueError:
        print("Hodnota musi byt cele cislo.")
        return 2

    fve = FvePowerControl()
    ok, msg = fve.connect()
    if not ok:
        print("Pripojenie ZLYHALO:", msg)
        return 1
    try:
        if raw_mode:
            ok, out = fve.set_raw(value)
            print(f"Zapis registra {fve.ctrl_register} = {value}")
        else:
            ok, out = fve.set_percent(value)
            print(f"Nastavenie {value} % -> register {fve.ctrl_register} = {value * fve.gain}")
        print(out)
        print("VYSLEDOK:", "OK" if ok else "ZLYHALO")
        return 0 if ok else 1
    finally:
        fve.close()


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
