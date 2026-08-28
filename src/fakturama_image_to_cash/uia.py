from pywinauto import Application

FAKTURAMA_PATH = r"C:\Program Files\Fakturama2\Fakturama.exe"
MAIN_WINDOW_CLASS = "SWT_Window0"
NEW_ORDER_BUTTON_NAME = "Create: New Order"


def connect_main_window(path: str = FAKTURAMA_PATH, timeout: int = 30):
    """Attach to the running Fakturama process and return its main window via UI Automation.

    Waits for the window to exist before restoring it: a cold Fakturama start can take
    a while to produce any window at all. Restore happens before the visible/ready wait
    because Fakturama can start minimized, which UIA correctly reports as not visible.
    """
    app = Application(backend="uia").connect(path=path)
    window = app.window(class_name=MAIN_WINDOW_CLASS, control_type="Window")
    window.wait("exists", timeout=timeout)
    window.restore()
    window.wait("visible ready", timeout=timeout)
    return window


def open_new_order(main_window, timeout: int = 10):
    """Invoke the top-toolbar New Order button and wait for its editor tab to appear."""
    button = main_window.child_window(
        title=NEW_ORDER_BUTTON_NAME, control_type="Button"
    ).wrapper_object()
    button.invoke()
    main_window.child_window(title="New Order", control_type="TabItem").wait("exists", timeout=timeout)
