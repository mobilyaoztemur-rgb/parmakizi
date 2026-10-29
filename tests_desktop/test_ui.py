from pathlib import Path
import csv
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from gateway.desktop import App
from gateway.storage import load_settings, save_settings


class DesktopTests(unittest.TestCase):
    def test_login_dashboard_and_tables_render(self):
        root = tk.Tk()
        with patch('gateway.desktop.load_settings', return_value=({}, {})), patch('gateway.desktop.suggested_subnet', return_value='192.168.1.0/24'):
            app = App(root, allow_local=True)
        root.update()
        self.assertEqual(root.title(), 'Parmakizi · Bulut cihaz yönetimi')
        app.client = type('Client', (), {'base_url': 'https://example.com'})()
        app.data = {'devices': [{'id': 'd', 'name': 'Test', 'brand': 'Demo', 'model': 'Sim', 'ip': '192.168.1.20', 'port': 4370, 'driver': 'simulator', 'gateway_name': 'PC', 'last_contact': None, 'last_error': None}], 'gateways': [], 'jobs': [], 'counts': {'users': 0, 'attendance': 0}}
        with patch.object(app, 'submit'):
            app.show_dashboard()
        root.update()
        self.assertEqual(len(app.notebook.tabs()), 7)
        self.assertEqual(len(app.devices.get_children()), 1)
        self.assertIn('1 cihaz', app.summary.get())
        app.close()

    def export_app(self):
        root = tk.Tk()
        with patch('gateway.desktop.load_settings', return_value=({}, {})), patch('gateway.desktop.suggested_subnet', return_value='192.168.1.0/24'):
            app = App(root, allow_local=True)
        app.client = Mock(base_url='https://example.com')
        app.data = {'devices': [{'id': 'd', 'name': 'Test', 'brand': 'Demo', 'model': 'Sim', 'ip': '192.168.1.20', 'port': 4370, 'driver': 'simulator', 'gateway_name': 'PC', 'last_contact': None, 'last_error': None}], 'gateways': [], 'jobs': [], 'counts': {'users': 0, 'attendance': 0}}
        with patch.object(app, 'submit'):
            app.show_dashboard()
        app.devices.selection_set('d')
        return app

    def test_csv_reads_all_pages_and_escapes_spreadsheet_formulas(self):
        app = self.export_app()
        app.client.request.side_effect = [
            {'records': [{'uid': 1, 'user_id': '1', 'name': '=SUM(A1)', 'card': 0, 'privilege': 0, 'synced_at': 'UTC'}], 'next': 1},
            {'records': [{'uid': 2, 'user_id': '2', 'name': 'Sonraki', 'card': 42, 'privilege': 0, 'synced_at': 'UTC'}], 'next': None}
        ]
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / 'records.csv'
            with patch('gateway.desktop.filedialog.asksaveasfilename', return_value=str(filename)), patch('gateway.desktop.messagebox.showinfo'), patch.object(app, 'submit', side_effect=lambda task, done: done(task())):
                app.export_records('users')
            with filename.open(encoding='utf-8-sig', newline='') as file:
                rows = list(csv.reader(file))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[1][2], "'=SUM(A1)")
            self.assertEqual(rows[2][2], 'Sonraki')
            self.assertIn('after=1', app.client.request.call_args.args[0])
        app.close()

    def test_failed_csv_export_preserves_existing_file(self):
        app = self.export_app()
        app.client.request.side_effect = [
            {'records': [{'uid': 1, 'user_id': '1', 'name': 'Test', 'card': 0, 'privilege': 0, 'synced_at': 'UTC'}], 'next': 1},
            ValueError('Connection failed')
        ]
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / 'records.csv'
            filename.write_text('Original file')
            with patch('gateway.desktop.filedialog.asksaveasfilename', return_value=str(filename)), patch.object(app, 'submit', side_effect=lambda task, done: done(task())), self.assertRaises(ValueError):
                app.export_records('users')
            self.assertEqual(filename.read_text(), 'Original file')
            self.assertEqual(list(Path(folder).iterdir()), [filename])
        app.close()


if __name__ == '__main__':
    unittest.main()
