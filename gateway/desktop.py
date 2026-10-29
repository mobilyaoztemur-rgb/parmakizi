"""Native Windows desktop UI. No browser, local web server, or inbound tunnel."""
from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import urllib.parse
import uuid

from gateway.client import CloudClient, ApiError
from gateway.connector import Connector, networks
from gateway.storage import load_settings, save_settings, dpapi

TYPES = {"device.info": "Cihaz bilgisi", "users.read": "Personel oku", "attendance.read": "Geçişleri oku", "users.upsert": "Personel yaz", "discover": "Ağ keşfi"}
STATES = {"queued": "Bekliyor", "running": "Çalışıyor", "succeeded": "Tamamlandı", "failed": "Başarısız", "uncertain": "Cihazdan kontrol edin"}


def default_url():
    path = Path(__file__).with_name("defaults.json")
    return json.loads(path.read_text()).get("cloud_url", "") if path.exists() else ""


def suggested_subnet():
    if sys.platform == "win32":
        try:
            command = "Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*'} | Select-Object IPAddress,PrefixLength | ConvertTo-Json -Compress"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
            rows = json.loads(result.stdout)
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                net = ipaddress.ip_network(f'{row["IPAddress"]}/{row["PrefixLength"]}', strict=False)
                try:
                    networks([str(net)])
                    return str(net)
                except ValueError:
                    continue
        except (OSError, ValueError, subprocess.SubprocessError, TypeError):
            pass
    return "192.168.1.0/24"


class App:
    def __init__(self, root, allow_local=False):
        self.root, self.allow_local = root, allow_local
        self.client = None
        self.connector = None
        self.data = {"devices": [], "gateways": [], "jobs": [], "counts": {}}
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.events = queue.Queue()
        self.busy = 0
        self.closed = False
        self.drain_after = None
        self.refresh_after = None
        self.read_next = None
        self.read_kind = None
        self.read_device = None
        try:
            self.preferences, self.secrets = load_settings()
        except (OSError, ValueError):
            self.preferences, self.secrets = {}, {}
        root.title("Parmakizi · Bulut cihaz yönetimi")
        root.geometry("1180x760")
        root.minsize(960, 640)
        root.configure(bg="#eef2f7")
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#eef2f7")
        style.configure("TLabel", background="#eef2f7", foreground="#13233e", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"))
        style.configure("Sub.TLabel", foreground="#61718b")
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 7))
        style.configure("Primary.TButton", background="#2563eb", foreground="white")
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=32, background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), padding=8)
        style.configure("TNotebook.Tab", padding=(16, 10))
        self.shell = ttk.Frame(root, padding=24)
        self.shell.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="Giriş bekleniyor")
        self.show_login()
        self.drain_after = root.after(100, self.drain)

    def submit(self, task, done=None):
        self.busy += 1
        future = self.executor.submit(task)
        future.add_done_callback(lambda f: self.events.put(("done", (f, done))))

    def drain(self):
        if self.closed:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status.set(payload)
                else:
                    future, done = payload
                    self.busy -= 1
                    try:
                        result = future.result()
                        if done:
                            done(result)
                    except Exception as exc:
                        messagebox.showerror("İşlem tamamlanamadı", str(exc) if isinstance(exc, (ApiError, ValueError)) else "İşlem başarısız. Bağlantıyı ve yerel ayarları kontrol edin.")
        except queue.Empty:
            pass
        self.drain_after = self.root.after(100, self.drain)

    def clear(self):
        for widget in self.shell.winfo_children():
            widget.destroy()

    def show_login(self):
        if self.refresh_after:
            self.root.after_cancel(self.refresh_after)
            self.refresh_after = None
        self.clear()
        frame = ttk.Frame(self.shell, padding=32)
        frame.place(relx=.5, rely=.5, anchor="center")
        ttk.Label(frame, text="◉  parmakizi", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Yerel cihazlarınız. Tek bir bulut merkezi.", style="Sub.TLabel").pack(anchor="w", pady=(4, 24))
        values = [self.preferences.get("cloud_url", default_url()), "", ", ".join(self.preferences.get("allowed_subnets", [suggested_subnet()]))]
        entries = []
        for label, value in zip(("Cloudflare API adresi", "Yönetici parolası", "İzin verilen yerel alt ağlar (virgülle ayırın)"), values):
            ttk.Label(frame, text=label).pack(anchor="w", pady=(12, 5))
            entry = ttk.Entry(frame, width=55, show="•" if label == "Yönetici parolası" else "")
            entry.insert(0, value)
            entry.pack(fill="x", ipady=5)
            entries.append(entry)
        ttk.Label(frame, text="Alt ağ önerisini kendi ağınızla karşılaştırın.\nCihaz iletişim şifreleri yalnızca bu Windows kullanıcısında saklanır.", style="Sub.TLabel").pack(anchor="w", pady=16)

        def login():
            url, password, subnet_text = entries[0].get().strip(), entries[1].get(), entries[2].get().strip()
            allowed = [x.strip() for x in subnet_text.split(",") if x.strip()]
            try:
                networks(allowed)
                client = CloudClient(url, allow_local=self.allow_local)
            except ValueError as exc:
                messagebox.showerror("Ayarları kontrol edin", str(exc))
                return
            if self.busy:
                return
            self.status.set("Buluta bağlanılıyor…")
            def task():
                client.request("/api/auth/login", {"password": password})
                return client.request("/api/overview")
            def connected(data):
                self.client = client
                if self.preferences.get("cloud_url") != client.base_url:
                    self.secrets = {}
                    self.preferences.pop("gateway_id", None)
                self.preferences.update(cloud_url=client.base_url, allowed_subnets=allowed)
                self.data = data
                self.show_dashboard()
                self.pair_and_start()
                self.refresh_after = self.root.after(10000, self.auto_refresh)
            self.submit(task, connected)

        ttk.Button(frame, text="Buluta bağlan  →", style="Primary.TButton", command=login).pack(fill="x", pady=8)
        entries[1].bind("<Return>", lambda _: login())
        ttk.Label(frame, textvariable=self.status, style="Sub.TLabel").pack(anchor="w", pady=8)

    def pair_and_start(self):
        gateway_id = self.preferences.get("gateway_id")
        valid = any(g["id"] == gateway_id and not g["revoked_at"] for g in self.data["gateways"])
        if valid and self.secrets.get("gateway_token"):
            self.start_connector()
            return
        def paired(data):
            self.preferences["gateway_id"] = data["id"]
            self.secrets["gateway_token"] = data["token"]
            self.start_connector()
            self.refresh()
        self.submit(lambda: self.client.request("/api/gateways", {"name": socket.gethostname() + " · Windows"}), paired)

    def start_connector(self):
        save_settings(self.preferences, self.secrets)
        config = dict(self.preferences, gateway_token=self.secrets["gateway_token"], device_passwords=self.secrets.get("device_passwords", {}), device_settings=self.secrets.get("device_settings", {}), timezone="Europe/Istanbul")
        self.connector = Connector(config, allow_local=self.allow_local, status=lambda s: self.events.put(("status", s)))
        threading.Thread(target=self.connector.run, daemon=True, name="LAN connector").start()

    def table(self, parent, columns, widths=None):
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True, pady=8)
        tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="browse")
        for i, column in enumerate(columns):
            tree.heading(column, text=column)
            tree.column(column, width=widths[i] if widths else 140, minwidth=65)
        scroll = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        return tree

    def show_dashboard(self):
        self.clear()
        top = ttk.Frame(self.shell)
        top.pack(fill="x")
        ttk.Label(top, text="◉  parmakizi", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Oturumu kapat", command=self.logout).pack(side="right")
        ttk.Button(top, text="Yenile", command=self.refresh).pack(side="right", padx=8)
        self.summary = tk.StringVar()
        ttk.Label(self.shell, textvariable=self.summary, style="Sub.TLabel").pack(anchor="w", pady=(12, 16))
        self.notebook = ttk.Notebook(self.shell)
        self.notebook.pack(fill="both", expand=True)
        tabs = []
        for title in ("Cihazlar", "Personel", "Geçiş kayıtları", "İşlemler", "Uyumluluk", "Bağlantı noktaları", "Denetim"):
            tab = ttk.Frame(self.notebook, padding=12)
            self.notebook.add(tab, text=title)
            tabs.append(tab)
        toolbar = ttk.Frame(tabs[0])
        toolbar.pack(fill="x")
        for label, command in [("+ Cihaz ekle", self.add_device), ("Ağda keşfet", self.scan), ("Cihaz bilgisi", lambda: self.job("device.info")), ("Personel oku", lambda: self.job("users.read")), ("Geçişleri oku", lambda: self.job("attendance.read")), ("Personel yaz", self.write_user), ("İletişim ayarı", self.device_secret)]:
            ttk.Button(toolbar, text=label, command=command).pack(side="left", padx=(0, 4))
        ttk.Label(tabs[0], text="Önce bir cihaz seçin. Simülatör kayıtları gerçek cihaz verisi değildir.", style="Sub.TLabel").pack(anchor="w", pady=(10, 0))
        self.devices = self.table(tabs[0], ("Cihaz", "Marka / model", "IP : port", "Protokol", "Bağlantı noktası", "Son başarılı işlem", "Son hata"))
        device_tools = ttk.Frame(tabs[0])
        device_tools.pack(fill='x')
        ttk.Button(device_tools, text='Seçili cihazı düzenle', command=self.edit_device).pack(side='left')
        ttk.Button(device_tools, text='Seçili cihazı buluttan kaldır', command=self.remove_device).pack(side='left', padx=8)
        self.users = self.table(tabs[1], ("UID", "Sicil", "Ad soyad", "Kart", "Cihaz yetkisi", "Son okuma"))
        self.attendance = self.table(tabs[2], ("Sicil", "Cihaz zamanı", "Saat dilimi", "UTC", "Durum", "Geçiş"))
        self.record_label = tk.StringVar(value="Cihazlar sekmesinde bir cihaz seçip kayıtları yükleyin.")
        for kind, tab in (("users", tabs[1]), ("attendance", tabs[2])):
            bar = ttk.Frame(tab)
            bar.pack(fill="x")
            ttk.Button(bar, text="Seçili cihazın bulut kayıtlarını yükle", command=lambda k=kind: self.read_records(k, reset=True)).pack(side="left")
            ttk.Button(bar, text="Sonraki sayfa", command=lambda k=kind: self.read_records(k)).pack(side="left", padx=8)
            ttk.Button(bar, text="Tüm kayıtları CSV kaydet", command=lambda k=kind: self.export_records(k)).pack(side='left', padx=(0, 8))
            ttk.Label(bar, textvariable=self.record_label, style="Sub.TLabel").pack(side="left")
        self.jobs = self.table(tabs[3], ("İşlem", "Cihaz", "Durum", "Başlangıç", "Hata / sonuç"), [140, 140, 140, 170, 400])
        self.jobs.bind("<Double-1>", self.job_details)
        ttk.Label(tabs[3], text="Keşif sonucundaki IP’leri görmek için işlemi çift tıklayın; adayları Cihaz ekle ile kaydedin.", style="Sub.TLabel").pack(anchor="w")
        self.catalog = self.table(tabs[4], ("Marka", "Aile / model", "Protokol", "Durum", "Açıklama"), [100, 230, 140, 160, 480])
        ttk.Label(tabs[4], text="Katalog, destek garantisi değildir. Seçili satırı çift tıklayarak kaynak ve doğrulama durumunu görebilirsiniz.", style="Sub.TLabel").pack(anchor="w")
        self.catalog.bind("<Double-1>", self.catalog_details)
        self.gateways = self.table(tabs[5], ("Bilgisayar", "Son bağlantı", "Anahtar durumu"))
        ttk.Button(tabs[5], text="Seçili bağlantı anahtarını iptal et", command=self.revoke).pack(anchor="w")
        self.audit = self.table(tabs[6], ("Zaman", "Kimlik", "İşlem", "Hedef"))
        ttk.Button(tabs[6], text="Denetim kayıtlarını yükle", command=lambda: self.submit(lambda: self.client.request("/api/audit"), self.render_audit)).pack(anchor="w")
        ttk.Label(self.shell, textvariable=self.status, style="Sub.TLabel").pack(anchor="w", pady=(12, 0))
        ttk.Label(self.shell, text="Yerel bağlantı bu uygulama açıkken çalışır. Biyometrik şablon aktarımı bu sürümde uygulanmamıştır.", style="Sub.TLabel").pack(anchor="w", pady=(4, 0))
        self.render(self.data)
        self.submit(lambda: self.client.request("/api/catalog"), self.render_catalog)

    def render(self, data):
        self.data = data
        counts = data["counts"]
        self.summary.set(f'{len(data["devices"])} cihaz   ·   {counts.get("users", 0)} personel kaydı   ·   {counts.get("attendance", 0)} geçiş kaydı   ·   {self.client.base_url}')
        selected = self.devices.selection()
        for tree in (self.devices, self.jobs, self.gateways):
            tree.delete(*tree.get_children())
        for d in data["devices"]:
            self.devices.insert("", "end", iid=d["id"], values=(d["name"], d["brand"] + " " + d["model"], f'{d["ip"]}:{d["port"]}', d["driver"], d["gateway_name"], d["last_contact"] or "Henüz yok", d["last_error"] or "—"))
        if selected and self.devices.exists(selected[0]):
            self.devices.selection_set(selected)
        for j in data["jobs"]:
            self.jobs.insert("", "end", iid=j["id"], values=(TYPES.get(j["type"], j["type"]), j["device_name"] or j["gateway_name"], STATES.get(j["status"], j["status"]), j["created_at"], j["error"] or j["result"]))
        for g in data["gateways"]:
            self.gateways.insert("", "end", iid=g["id"], values=(g["name"], g["last_seen"] or "Henüz bağlanmadı", "İptal edildi" if g["revoked_at"] else "Aktif"))

    def refresh(self):
        if self.client:
            self.submit(lambda: self.client.request("/api/overview"), self.render)

    def auto_refresh(self):
        self.refresh_after = None
        if self.closed or not self.client:
            return
        if not self.busy:
            self.refresh()
        self.refresh_after = self.root.after(10000, self.auto_refresh)

    def selected_device(self):
        selected = self.devices.selection()
        if not selected:
            messagebox.showinfo("Cihaz seçin", "Cihazlar sekmesinde bir cihaz seçin.")
            return None
        return next(d for d in self.data["devices"] if d["id"] == selected[0])

    def enqueue(self, payload):
        payload["requestId"] = str(uuid.uuid4())
        self.submit(lambda: self.client.request("/api/jobs", payload, retries=2), lambda _: self.refresh())

    def job(self, kind):
        device = self.selected_device()
        if device:
            self.enqueue({"type": kind, "deviceId": device["id"]})

    def form(self, title, fields, submit):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=24)
        frame.pack(fill="both", expand=True)
        entries = {}
        for key, label, value in fields:
            ttk.Label(frame, text=label).pack(anchor="w", pady=(8, 4))
            if isinstance(value, list):
                entry = ttk.Combobox(frame, values=value, state="readonly", width=42)
                if value:
                    entry.current(0)
            else:
                entry = ttk.Entry(frame, width=45)
                entry.insert(0, str(value))
                if key == 'password':
                    entry.configure(show='•')
            entry.pack(fill="x", ipady=3)
            entries[key] = entry
        def confirm():
            try:
                submit({k: e.get().strip() for k, e in entries.items()})
                dialog.destroy()
            except (ValueError, KeyError) as exc:
                messagebox.showerror("Alanları kontrol edin", str(exc), parent=dialog)
        ttk.Button(frame, text="Kaydet / gönder", style="Primary.TButton", command=confirm).pack(fill="x", pady=(20, 0))

    def add_device(self):
        active = [g for g in self.data["gateways"] if not g["revoked_at"]]
        if not active:
            return messagebox.showinfo("Bağlantı bekleniyor", "Önce bu bilgisayarın bulut eşleştirmesi tamamlanmalı.")
        active.sort(key=lambda g: g["id"] != self.preferences.get("gateway_id"))
        choices = {g["name"] + " · " + g["id"][:8]: g["id"] for g in active}
        def send(values):
            values["gatewayId"] = choices[values.pop("gateway")]
            values["port"] = int(values["port"])
            self.submit(lambda: self.client.request("/api/devices", values), lambda _: self.refresh())
        self.form("Cihaz ekle", [("name", "Cihaz adı", ""), ("brand", "Marka", "ZKTeco"), ("model", "Model", ""), ("ip", "Yerel IPv4 adresi", "192.168.1.201"), ("port", "İletişim portu", "4370"), ("driver", "Protokol (simulator yalnızca demo)", ["zkteco", "simulator"]), ("gateway", "Cihazla aynı ağdaki bilgisayar", list(choices))], send)

    def scan(self):
        if not self.preferences.get("gateway_id"):
            return
        self.form("Yerel ağ keşfi · TCP portu adayları", [("cidr", "İzinli alt ağ (/24–/30)", self.preferences["allowed_subnets"][0]), ("port", "Aranacak TCP portu", "4370")], lambda v: self.enqueue({"type": "discover", "gatewayId": self.preferences["gateway_id"], "cidr": v["cidr"], "port": int(v["port"])}))

    def edit_device(self):
        device = self.selected_device()
        if not device:
            return
        def send(values):
            values['port'] = int(values['port'])
            moved = values['ip'] != device['ip'] or values['port'] != device['port']
            if moved and not messagebox.askyesno('Cihaz adresi değişecek', 'Bu kaydın IP/portu değiştirilecek. Önceden okunan bulut kayıtları korunur. Yeni adres aynı fiziksel cihaza mı ait?'):
                return
            self.submit(lambda: self.client.request(f'/api/devices/{device["id"]}', values, method='PATCH'), lambda _: self.refresh())
        self.form('Kayıtlı cihazı düzenle', [(key, label, device[key]) for key, label in [('name', 'Cihaz adı'), ('brand', 'Marka'), ('model', 'Model'), ('ip', 'Yerel IPv4 adresi'), ('port', 'Port')]], send)

    def remove_device(self):
        device = self.selected_device()
        if not device:
            return
        if not messagebox.askyesno('Bulut kaydını kaldır', f'{device["name"]} ve bu cihaza ait buluttaki personel, geçiş ve işlem kayıtları silinsin mi?\n\nFiziksel cihazdaki bilgiler değişmez. Bu işlem geri alınamaz.'):
            return
        def done(_):
            self.read_next = self.read_kind = self.read_device = None
            self.users.delete(*self.users.get_children())
            self.attendance.delete(*self.attendance.get_children())
            self.refresh()
        self.submit(lambda: self.client.request(f'/api/devices/{device["id"]}', {}, method='DELETE'), done)

    def write_user(self):
        device = self.selected_device()
        if not device:
            return
        def send(v):
            for key in ("uid", "card"):
                v[key] = int(v[key])
            if messagebox.askyesno("Cihaz kaydını yaz", f'{device["name"]} cihazına UID {v["uid"]} yazılsın mı?\nMevcut aynı UID’nin adı, sicili ve kartı güncellenir. Cihaz yetkisi ve parolası korunur.'):
                self.enqueue(dict(v, type="users.upsert", deviceId=device["id"]))
        self.form("Cihaza personel yaz", [("uid", "Cihaz UID (1–65535)", ""), ("userId", "Sicil numarası (en fazla 9 rakam)", ""), ("name", "Ad soyad (en fazla 24 UTF-8 baytı)", ""), ("card", "Kart numarası (0 = kart yok)", "0")], send)

    def device_secret(self):
        device = self.selected_device()
        if not device or device["gateway_id"] != self.preferences.get("gateway_id"):
            return messagebox.showinfo("Yerel ayar", "İletişim şifresini cihazın bağlı olduğu bilgisayarda ayarlayın.")
        key = f'{device["ip"]}:{device["port"]}'
        options = self.secrets.get('device_settings', {}).get(key, {})
        def save(v):
            number = int(v['password'])
            if not 0 <= number <= 4294967295:
                raise ValueError('Şifre 0–4294967295 arasında olmalı.')
            settings = self.secrets.setdefault('device_settings', {})
            settings[key] = {'password': number, 'force_udp': v['transport'] == 'Yalnızca UDP', 'encoding': v['encoding']}
            save_settings(self.preferences, self.secrets)
            if self.connector:
                self.connector.zk.config['device_settings'] = dict(settings)
        transport = ['Otomatik TCP / UDP', 'Yalnızca UDP']
        if options.get('force_udp'):
            transport.reverse()
        encodings = ['utf-8', 'cp1254', 'latin-1', 'gbk']
        encodings.sort(key=lambda x: x != options.get('encoding', 'utf-8'))
        self.form('Yerel cihaz iletişimi · buluta gönderilmez', [('password', 'ZK iletişim şifresi', options.get('password', 0)), ('transport', 'Bağlantı türü', transport), ('encoding', 'Cihaz karakter kodlaması', encodings)], save)

    def read_records(self, kind, reset=False):
        device = self.selected_device() if reset else None
        if reset and not device:
            return
        if reset:
            self.read_kind, self.read_device, self.read_next = kind, device["id"], None
        elif self.read_kind != kind or self.read_next is None:
            return messagebox.showinfo("Sayfalama", "Önce kayıtları yükleyin veya son sayfadasınız.")
        params = {"deviceId": self.read_device}
        if self.read_next is not None:
            params["after" if kind == "users" else "before"] = self.read_next
        path = f'/api/{kind}?' + urllib.parse.urlencode(params)
        def render(result):
            tree = self.users if kind == "users" else self.attendance
            tree.delete(*tree.get_children())
            for row in result["records"]:
                keys = ("uid", "user_id", "name", "card", "privilege", "synced_at") if kind == "users" else ("user_id", "device_local_time", "timezone", "recorded_at", "status", "punch")
                tree.insert("", "end", values=tuple(row[k] for k in keys))
            self.read_next = result["next"]
            self.record_label.set(f'{len(result["records"])} kayıt · ' + ("Devamı var" if result["next"] is not None else "Son sayfa"))
            self.notebook.select(1 if kind == "users" else 2)
        self.submit(lambda: self.client.request(path), render)

    def render_catalog(self, result):
        self.catalog_data = result["catalog"]
        self.catalog.delete(*self.catalog.get_children())
        for i, row in enumerate(self.catalog_data):
            status = {"adapter": "Adaptör var · saha testi yok", "research": "Protokol tespiti gerekli", "planned": "Adaptör planlanıyor"}[row["status"]]
            self.catalog.insert("", "end", iid=str(i), values=(row["brand"], row["family"], row["protocol"], status, row["note"]))

    def export_records(self, kind):
        device = self.selected_device()
        if not device:
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title='Bulut kayıtlarını CSV kaydet', defaultextension='.csv', filetypes=[('CSV', '*.csv')], initialfile='parmakizi-' + kind + '.csv', confirmoverwrite=True)
        if not filename:
            return
        def task():
            headers = ('uid', 'user_id', 'name', 'card', 'privilege', 'synced_at') if kind == 'users' else ('user_id', 'device_local_time', 'timezone', 'recorded_at', 'status', 'punch')
            path = Path(filename)
            temporary = path.with_name('.' + path.name + '.' + str(uuid.uuid4()) + '.tmp')
            count = 0
            seen = set()
            cursor = None
            def safe(value):
                value = '' if value is None else str(value)
                return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value
            try:
                with temporary.open('x', newline='', encoding='utf-8-sig') as file:
                    writer = csv.writer(file)
                    writer.writerow(headers)
                    while True:
                        params = {'deviceId': device['id']}
                        if cursor is not None:
                            params['after' if kind == 'users' else 'before'] = cursor
                        result = self.client.request(f'/api/{kind}?' + urllib.parse.urlencode(params))
                        for row in result['records']:
                            writer.writerow(safe(row[h]) for h in headers)
                            count += 1
                        cursor = result['next']
                        if cursor is None:
                            break
                        if cursor in seen:
                            raise ValueError('Dışa aktarma sayfası tekrarladı; dosya tamamlanmadı.')
                        seen.add(cursor)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return count
        self.submit(task, lambda count: messagebox.showinfo('CSV kaydedildi', f'{count} bulut kaydı dışa aktarıldı.'))

    def catalog_details(self, _):
        selected = self.catalog.selection()
        if selected:
            r = self.catalog_data[int(selected[0])]
            messagebox.showinfo(r["brand"], f'{r["models"]}\n\n{r["note"]}\n\n{r["evidence"]}\n\nKaynak: {r["source"]}')

    def job_details(self, _):
        selected = self.jobs.selection()
        if selected:
            job = next(j for j in self.data["jobs"] if j["id"] == selected[0])
            messagebox.showinfo(TYPES.get(job["type"], job["type"]), job["error"] or json.dumps(json.loads(job["result"]), indent=2, ensure_ascii=False))

    def render_audit(self, result):
        self.audit.delete(*self.audit.get_children())
        for r in result["results"]:
            self.audit.insert("", "end", values=(r["created_at"], r["actor"], r["action"], r["target"]))

    def revoke(self):
        selected = self.gateways.selection()
        if selected and messagebox.askyesno("Anahtarı iptal et", "Bu bilgisayarın cihaz bağlantısı kesilsin mi? Yeniden eşleştirme için tekrar giriş gerekir."):
            self.submit(lambda: self.client.request(f'/api/gateways/{selected[0]}/revoke', {}), lambda _: self.refresh())

    def logout(self):
        if self.busy:
            return messagebox.showinfo("İşlem sürüyor", "Devam eden bulut isteğinin tamamlanmasını bekleyin.")
        if self.connector:
            self.connector.stop.set()
        def done(_):
            self.client = None
            self.status.set("Oturum kapatıldı")
            self.show_login()
        self.submit(lambda: self.client.request("/api/auth/logout", {}), done)

    def close(self):
        self.closed = True
        for callback in (self.drain_after, self.refresh_after):
            if callback:
                self.root.after_cancel(callback)
        if self.connector:
            self.connector.stop.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-insecure-local", action="store_true")
    parser.add_argument('--self-test-report', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test_report:
        report = {'ok': False}
        app = None
        try:
            root = tk.Tk()
            root.withdraw()
            app = App(root)
            root.update_idletasks()
            report['window_title'] = root.title()
            endpoint = default_url()
            if not endpoint:
                raise ValueError('Packaged default endpoint missing')
            report['default_endpoint'] = endpoint
            client = CloudClient(endpoint)
            report['health'] = client.request('/api/health')
            if report['health'].get('ok') is not True:
                raise ValueError('Cloud health failed')
            try:
                client.request('/api/overview')
            except ApiError as error:
                report['anonymous_http_status'] = error.status
                if error.status != 401:
                    raise
            else:
                raise ValueError('Anonymous cloud access was not rejected')
            if sys.platform == 'win32':
                sample = secrets.token_bytes(24)
                report['windows_dpapi'] = dpapi(dpapi(sample), decrypt=True) == sample
                if not report['windows_dpapi']:
                    raise ValueError('Windows credential protection failed')
            report['ok'] = True
        except Exception as error:
            report['error_type'] = type(error).__name__
        finally:
            if app:
                app.close()
            Path(args.self_test_report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        raise SystemExit(0 if report['ok'] else 1)
    root = tk.Tk()
    App(root, args.allow_insecure_local)
    root.mainloop()


if __name__ == "__main__":
    main()
