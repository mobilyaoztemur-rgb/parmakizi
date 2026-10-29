"""A bounded LAN worker. All device credentials remain on the local machine."""
from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import contextmanager
from datetime import datetime
import ipaddress
import json
import logging
from pathlib import Path
import socket
import threading
from zoneinfo import ZoneInfo

from gateway.client import CloudClient, ApiError

LOG = logging.getLogger("parmakizi.connector")
PRIVATE = tuple(ipaddress.ip_network(x) for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def networks(values):
    if not values:
        raise ValueError("En az bir izinli yerel alt ağ tanımlayın.")
    result = []
    for value in values:
        net = ipaddress.ip_network(value, strict=True)
        if net.version != 4 or not any(net.subnet_of(x) for x in PRIVATE):
            raise ValueError("Yalnızca özel IPv4 alt ağlarına izin verilir.")
        result.append(net)
    return result


def allowed_ip(value, allowed):
    address = ipaddress.ip_address(value)
    if address.version != 4 or not any(address in n for n in allowed):
        raise ValueError("Cihaz IP’si bu bilgisayardaki izinli alt ağların dışında.")
    return str(address)


def discover(cidr, port, allowed, stop=None):
    target = ipaddress.ip_network(cidr, strict=True)
    if target.version != 4 or not 24 <= target.prefixlen <= 30 or not any(target.subnet_of(n) for n in allowed):
        raise ValueError("Keşif alt ağı izinli olmalı ve /24–/30 aralığında olmalı.")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Port geçersiz.")

    def probe(address):
        if stop and stop.is_set():
            return None
        try:
            with socket.create_connection((str(address), port), timeout=0.4):
                return {"ip": str(address), "port": port, "kind": "tcp_candidate"}
        except OSError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        return [r for r in pool.map(probe, target.hosts()) if r]


class Simulator:
    """Explicit demo driver, never selected based on connection failures."""
    def __init__(self):
        self.users_by_device = {}

    def execute(self, device, action, params):
        users = self.users_by_device.setdefault(device["id"], [{"uid": 1, "userId": "1001", "name": "Demo Kullanıcı", "card": 0, "privilege": 0}])
        if action == "device.info":
            return [], {"serial": "SIM-" + device["id"][:8], "platform": "Simülatör", "firmware": "demo-1", "users": len(users), "simulated": True}
        if action == "users.read":
            return [dict(x) for x in users], {"count": len(users), "simulated": True}
        if action == "attendance.read":
            records = [{"userId": "1001", "recordedAt": "2026-10-29T06:00:00+00:00", "localTime": "2026-10-29T09:00:00", "timezone": "Europe/Istanbul", "status": 1, "punch": 0}]
            return records, {"count": len(records), "simulated": True}
        if action == "users.upsert":
            existing = next((u for u in users if u["uid"] == params["uid"]), None)
            if any(u["userId"] == params["userId"] and u["uid"] != params["uid"] for u in users):
                raise ValueError("Sicil numarası farklı bir UID’de kayıtlı.")
            if existing:
                existing.update(params)
            else:
                users.append(dict(params, privilege=0))
            return [], {"uid": params["uid"], "written": True, "simulated": True}
        raise ValueError("Desteklenmeyen demo komutu.")


class ZktecoAdapter:
    def __init__(self, config):
        self.config = config
        self.timezone = ZoneInfo(config.get("timezone", "Europe/Istanbul"))

    @contextmanager
    def connection(self, device):
        from zk import ZK
        options = self.config.get("device_settings", {}).get(f'{device["ip"]}:{device["port"]}', {})
        password = options.get("password", self.config.get("device_passwords", {}).get(device["ip"], 0))
        if not isinstance(password, int) or not 0 <= password <= 4294967295:
            raise ValueError("Cihaz iletişim şifresi 32-bit pozitif tamsayı olmalı.")
        conn = None
        try:
            conn = ZK(device["ip"], port=device["port"], timeout=20, password=password,
                      force_udp=bool(options.get("force_udp", self.config.get("force_udp", False))), ommit_ping=True,
                      encoding=options.get("encoding", self.config.get("encoding", "utf-8"))).connect()
            # Do not disable attendance collection or change the clock while reading.
            yield conn
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    LOG.warning("Cihaz bağlantısı kapatılırken hata oluştu.")

    def execute(self, device, action, params):
        with self.connection(device) as conn:
            if action == "device.info":
                return [], {"serial": str(conn.get_serialnumber()), "platform": str(conn.get_platform()), "firmware": str(conn.get_firmware_version())}
            if action == "users.read":
                records = [{"uid": int(u.uid), "userId": str(u.user_id), "name": u.name, "card": int(u.card or 0), "privilege": int(u.privilege)} for u in conn.get_users()]
                return records, {"count": len(records)}
            if action == "attendance.read":
                records = []
                for r in conn.get_attendance():
                    local = r.timestamp
                    aware = local.replace(tzinfo=self.timezone) if local.tzinfo is None else local
                    records.append({"userId": str(r.user_id), "recordedAt": aware.isoformat(), "localTime": local.replace(tzinfo=None).isoformat(), "timezone": str(self.timezone), "status": int(r.status), "punch": int(r.punch)})
                return records, {"count": len(records)}
            if action == "users.upsert":
                users = conn.get_users()
                existing = next((u for u in users if int(u.uid) == params["uid"]), None)
                if any(str(u.user_id) == params["userId"] and int(u.uid) != params["uid"] for u in users):
                    raise ValueError("Sicil numarası farklı bir UID’de kayıtlı.")
                conn.set_user(uid=params["uid"], name=params["name"], user_id=params["userId"], card=params["card"],
                              privilege=int(existing.privilege) if existing else 0,
                              password=existing.password if existing else "",
                              group_id=existing.group_id if existing else "")
                observed = next((u for u in conn.get_users() if int(u.uid) == params["uid"]), None)
                if not observed or observed.name != params["name"] or str(observed.user_id) != params["userId"] or int(observed.card or 0) != params["card"]:
                    raise ValueError("Yazma sonucu cihazdan doğrulanamadı; cihaz kaydını kontrol edin.")
                return [], {"uid": params["uid"], "written": True, "verified": True}
        raise ValueError("Desteklenmeyen ZK komutu.")


class Connector:
    def __init__(self, config, allow_local=False, status=None):
        self.config = config
        self.allowed = networks(config["allowed_subnets"])
        self.client = CloudClient(config["cloud_url"], config["gateway_token"], allow_local)
        self.stop = threading.Event()
        self.simulator = Simulator()
        self.zk = ZktecoAdapter(config)
        self.status = status or (lambda message: LOG.info("%s", message))

    def execute(self, job):
        if job["type"] == "discover":
            found = discover(job["params"]["cidr"], job["params"]["port"], self.allowed, self.stop)
            return [], {"candidates": found, "count": len(found), "note": "Açık TCP portu adayları; biyometrik cihaz veya marka doğrulaması değildir. UDP cihazlar görünmeyebilir."}
        device = job["device"]
        allowed_ip(device["ip"], self.allowed)
        if device["driver"] == "simulator":
            return self.simulator.execute(device, job["type"], job["params"])
        if device["driver"] == "zkteco":
            return self.zk.execute(device, job["type"], job["params"])
        raise ValueError("Bu cihazın protokolü desteklenmiyor.")

    def process(self, job):
        path = f'/api/gateway/jobs/{job["id"]}/result'
        try:
            self.status("Çalışıyor: " + job["type"])
            records, summary = self.execute(job)
        except Exception:
            # SDK errors can contain packet payloads or credentials. Keep the public error generic.
            self.client.request(path, {"claimToken": job["claimToken"], "final": True,
                                     "error": "Cihaz işlemi tamamlanamadı. IP, izinli alt ağ, protokol ve yerel iletişim şifresini kontrol edin. Yazma işlemini yeniden göndermeden önce cihazı okuyun."})
            self.status("Cihaz işlemi başarısız: " + job["type"])
            return
        # A failed upload must never re-execute the device command. Only retry these idempotent uploads.
        for offset in range(0, len(records), 40):
            if self.stop.is_set():
                return
            self.client.request(path, {"claimToken": job["claimToken"], "final": False, "records": records[offset:offset + 40]}, retries=3)
        self.client.request(path, {"claimToken": job["claimToken"], "final": True, "summary": summary}, retries=3)
        self.status("Tamamlandı: " + job["type"])

    def run(self, once=False):
        delay = 3
        while not self.stop.is_set():
            try:
                result = self.client.request("/api/gateway/poll", {})
                delay = 3
                if result["job"]:
                    self.process(result["job"])
                else:
                    self.status("Buluta bağlı · görev bekleniyor")
            except ApiError as exc:
                self.status(str(exc))
                if exc.status == 401:
                    self.status("Bağlantı anahtarı iptal edilmiş; yeniden eşleştirin.")
                    return
                delay = min(delay * 2, 60)
            except Exception:
                self.status("Bağlantı hatası; güvenli yeniden deneme bekleniyor.")
                delay = min(delay * 2, 60)
            if once:
                return
            self.stop.wait(delay)


def main():
    parser = argparse.ArgumentParser(description="Parmakizi yerel bağlantı servisi")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--allow-insecure-local", action="store_true", help="Yalnızca localhost geliştirme sunucusu")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    connector = Connector(config, args.allow_insecure_local)
    try:
        connector.run(args.once)
    except KeyboardInterrupt:
        connector.stop.set()


if __name__ == "__main__":
    main()
