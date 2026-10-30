# Third-party components

The ZK adapter uses **pyzk 0.9**, an unofficial ZKTeco library by Fanani M. Ihsan and contributors, distributed under **GPL version 2**. Source and license: https://github.com/fananimi/pyzk . This is not an official ZKTeco SDK. The GPL license text is included in `docs/licenses/pyzk-GPL-2.0.txt`.

Bundled Python dependencies also include **future 1.0.0** (MIT, Python Charmers), **tzdata 2025.2** (Apache-2.0, Python Software Foundation / Paul Ganssle and contributors; underlying IANA time zone data includes public-domain material), **cryptography 46.0.3** (Apache-2.0 or BSD-3-Clause), **cffi 2.0.0** (MIT), and **pycparser 2.23** (BSD-3-Clause). Python, Tcl/Tk, OpenSSL, and PyInstaller have their own licenses. PyInstaller's bootloader exception applies to applications built using it.

The application itself has no project-wide distribution license selected yet. Keep the initial binary within the project team; choose a distribution license compatible with bundled dependencies, or replace the GPL adapter with an appropriately licensed SDK, before distributing a product externally. Do not remove third-party notices. Manufacturer trademarks identify compatibility research and do not indicate affiliation or certification.
