from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
