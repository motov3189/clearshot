"""A private D-Bus portal tests the actual v2 request and asynchronous response."""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ScreenshotPortal, Gio, GLib

XML = '''<node><interface name="org.freedesktop.portal.Screenshot">
<method name="Screenshot"><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/>
<arg type="o" direction="out"/></method></interface></node>'''


class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        cls.bus.up()
        cls.server = Gio.DBusConnection.new_for_address_sync(cls.bus.get_bus_address(),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        cls.client = Gio.DBusConnection.new_for_address_sync(cls.bus.get_bus_address(),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        cls.server.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'RequestName', GLib.Variant('(su)', ('org.freedesktop.portal.Desktop', 0)), None,
            Gio.DBusCallFlags.NONE, 2000, None)
        cls.registration = cls.server.register_object('/org/freedesktop/portal/desktop',
            Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], cls.serve, None, None)

    @classmethod
    def serve(cls, connection, sender, path, interface, method, parameters, invocation):
        parent, options = parameters.unpack()
        cls.options = options
        request = '/org/freedesktop/portal/desktop/request/' + sender.lstrip(':').replace('.', '_') + '/' + options['handle_token']
        # Deliberately send the signal before the method reply (the real race).
        connection.emit_signal(sender, request, 'org.freedesktop.portal.Request', 'Response',
            GLib.Variant('(ua{sv})', (cls.code, {'uri': GLib.Variant('s', cls.uri)})))
        invocation.return_value(GLib.Variant('(o)', (request,)))

    def request(self, code=0, uri='file:///tmp/screen%20shot.png'):
        type(self).code, type(self).uri = code, uri
        loop = GLib.MainLoop()
        result = []
        def done(path, error):
            result.append((path, error))
            loop.quit()
        portal = ScreenshotPortal(done, connection=self.client)
        portal.start()
        timer = GLib.timeout_add(2000, lambda: (loop.quit(), False)[1])
        loop.run()
        GLib.source_remove(timer)
        self.assertEqual(len(result), 1, 'Portal response was lost')
        self.assertEqual(portal.subscription, 0)
        self.assertEqual(portal.timer, 0)
        portal._finish(None, 'duplicate')
        self.assertEqual(len(result), 1)
        return result[0]

    def test_fast_response_and_pixel_source_uri(self):
        path, error = self.request()
        self.assertEqual(path, Path('/tmp/screen shot.png'))
        self.assertIsNone(error)
        self.assertTrue(self.options['interactive'])
        self.assertFalse(self.options['modal'])

    def test_cancel_is_not_an_error(self):
        self.assertEqual(self.request(code=1), (None, None))

    def test_backend_failure_reports_error(self):
        path, error = self.request(code=2)
        self.assertIsNone(path)
        self.assertTrue(error)

    def test_reject_remote_uri(self):
        path, error = self.request(uri='file://remote-host/tmp/example.png')
        self.assertIsNone(path)
        self.assertTrue(error)

    @classmethod
    def tearDownClass(cls):
        cls.server.unregister_object(cls.registration)
        cls.server.close_sync(None)
        cls.client.close_sync(None)
        # TestDBus.stop avoids waiting on Gio's process-wide cached client.
        cls.bus.stop()


if __name__ == '__main__':
    unittest.main()
