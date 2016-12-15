
import os
import sys

from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtWidgets import QApplication, QMessageBox

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import host, makedirs
from eventlib import api
from zope.interface import implements

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.addressbook import Contact, Group
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads import XMLDocument
from sipsimple.storage import FileStorage
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread

from blink.__info__ import __project__, __summary__, __webpage__, __version__, __date__, __author__, __email__, __license__, __copyright__

try:
    from blink import branding
except ImportError:
    branding = Null

from blink.chatwindow import ChatWindow
from blink.configuration.account import AccountExtension, BonjourAccountExtension
from blink.configuration.addressbook import ContactExtension, GroupExtension
from blink.configuration.settings import SIPSimpleSettingsExtension
from blink.logging import LogManager
from blink.mainwindow import MainWindow
from blink.presence import PresenceManager
from blink.resources import ApplicationData, Resources
from blink.sessions import SessionManager
from blink.update import UpdateManager
from blink.util import QSingleton, run_in_gui_thread


__all__ = ['Blink']


if hasattr(sys, 'frozen'):
    makedirs(ApplicationData.get('logs'))
    sys.stdout.file = open(ApplicationData.get('logs/output.log'), 'a', 0)
    import httplib2
    httplib2.CA_CERTS = os.environ['SSL_CERT_FILE'] = Resources.get('tls/cacerts.pem')


class IPAddressMonitor(object):
    """
    An object which monitors the IP address used for the default route of the
    host and posts a SystemIPAddressDidChange notification when a change is
    detected.
    """

    def __init__(self):
        self.greenlet = None

    @run_in_green_thread
    def start(self):
        notification_center = NotificationCenter()

        if self.greenlet is not None:
            return
        self.greenlet = api.getcurrent()

        current_address = host.default_ip
        while True:
            new_address = host.default_ip
            # make sure the address stabilized
            api.sleep(5)
            if new_address != host.default_ip:
                continue
            if new_address != current_address:
                notification_center.post_notification(name='SystemIPAddressDidChange', sender=self, data=NotificationData(old_ip_address=current_address, new_ip_address=new_address))
                current_address = new_address
            api.sleep(5)

    @run_in_twisted_thread
    def stop(self):
        if self.greenlet is not None:
            api.kill(self.greenlet, api.GreenletExit())
            self.greenlet = None


class Blink(QApplication):
    __metaclass__ = QSingleton

    implements(IObserver)

    def __init__(self):
        super(Blink, self).__init__(sys.argv)
        self.setAttribute(Qt.AA_DontShowIconsInMenus, False)
        self.sip_application = SIPApplication()
        self.first_run = False

        self.setOrganizationDomain("ag-projects.com")
        self.setOrganizationName("AG Projects")
        self.setApplicationName("Blink")
        self.setApplicationVersion(__version__)

        self.main_window = MainWindow()
        self.chat_window = ChatWindow()
        self.main_window.__closed__ = True
        self.chat_window.__closed__ = True
        self.main_window.installEventFilter(self)
        self.chat_window.installEventFilter(self)

        self.main_window.addAction(self.chat_window.control_button.actions.main_window)
        self.chat_window.addAction(self.main_window.quit_action)
        self.chat_window.addAction(self.main_window.help_action)
        self.chat_window.addAction(self.main_window.redial_action)
        self.chat_window.addAction(self.main_window.join_conference_action)
        self.chat_window.addAction(self.main_window.mute_action)
        self.chat_window.addAction(self.main_window.silent_action)
        self.chat_window.addAction(self.main_window.preferences_action)
        self.chat_window.addAction(self.main_window.transfers_window_action)
        self.chat_window.addAction(self.main_window.logs_window_action)
        self.chat_window.addAction(self.main_window.received_files_window_action)
        self.chat_window.addAction(self.main_window.screenshots_window_action)

        self.ip_address_monitor = IPAddressMonitor()
        self.log_manager = LogManager()
        self.presence_manager = PresenceManager()
        self.session_manager = SessionManager()
        self.update_manager = UpdateManager()

        # Prevent application from exiting after last window is closed if system tray was initialized
        if self.main_window.system_tray_icon:
            self.setQuitOnLastWindowClosed(False)

        self.main_window.check_for_updates_action.triggered.connect(self.update_manager.check_for_updates)
        self.main_window.check_for_updates_action.setVisible(self.update_manager != Null)

        if getattr(sys, 'frozen', False):
            XMLDocument.schema_path = Resources.get('xml-schemas')

        Account.register_extension(AccountExtension)
        BonjourAccount.register_extension(BonjourAccountExtension)
        Contact.register_extension(ContactExtension)
        Group.register_extension(GroupExtension)
        SIPSimpleSettings.register_extension(SIPSimpleSettingsExtension)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.sip_application)

        branding.setup(self)

    def run(self):
        self.first_run = not os.path.exists(ApplicationData.get('config'))
        self.sip_application.start(FileStorage(ApplicationData.directory))
        self.exec_()
        self.update_manager.shutdown()
        self.sip_application.stop()
        self.sip_application.thread.join()
        self.log_manager.stop()

    def quit(self):
        self.chat_window.close()
        self.main_window.close()
        super(Blink, self).quit()

    def eventFilter(self, watched, event):
        if watched in (self.main_window, self.chat_window):
            if event.type() == QEvent.Show:
                watched.__closed__ = False
            elif event.type() == QEvent.Close:
                watched.__closed__ = True
                if self.main_window.__closed__ and self.chat_window.__closed__:
                    # close auxiliary windows
                    self.main_window.conference_dialog.close()
                    self.main_window.filetransfer_window.close()
                    self.main_window.preferences_window.close()
        return False

    def customEvent(self, event):
        handler = getattr(self, '_EH_%s' % event.name, Null)
        handler(event)

    def _EH_CallFunctionEvent(self, event):
        try:
            event.function(*event.args, **event.kw)
        except:
            log.exception('Exception occurred while calling function %s in the GUI thread' % event.function.__name__)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        self.log_manager.start()
        self.presence_manager.start()

    @run_in_gui_thread
    def _NH_SIPApplicationDidStart(self, notification):
        self.ip_address_monitor.start()
        self.main_window.show()
        accounts = AccountManager().get_accounts()
        if not accounts or (self.first_run and accounts == [BonjourAccount()]):
            self.main_window.preferences_window.show_create_account_dialog()
        self.update_manager.initialize()

    def _NH_SIPApplicationWillEnd(self, notification):
        self.ip_address_monitor.stop()

    def _NH_SIPApplicationDidEnd(self, notification):
        self.presence_manager.stop()

    @run_in_gui_thread
    def _NH_SIPApplicationGotFatalError(self, notification):
        log.error('Fatal error:\n{}'.format(notification.data.traceback))
        QMessageBox.critical(self.main_window, u"Fatal Error", u"A fatal error occurred, {} will now exit.".format(self.applicationName()))
        sys.exit(1)

