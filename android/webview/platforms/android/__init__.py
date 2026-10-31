import json
import logging
from http.cookies import SimpleCookie
from threading import Semaphore, Timer
from urllib.parse import urlparse

from android.activity import _activity as activity  # noqa
from android.runnable import run_on_ui_thread  # noqa
from jnius import autoclass, cast

from webview import _state, settings
from webview.models import Request, Response
from webview.platforms.android.app import App
from webview.platforms.android.jclass import (
    AlertDialogBuilder,
    Context,
    CookieManager,
    DownloadManagerRequest,
    Environment,
    KeyEvent,
    PyJavascriptInterface,
    PyWebChromeClient,
    PyWebViewClient,
    Uri,
    View,
    WebView,
)
from webview.platforms.android.jinterface import (
    DownloadListener,
    EventCallbackWrapper,
    JsApiCallbackWrapper,
    KeyListener,
    RequestInterceptor,
    ValueCallback,
)
from webview.util import create_cookie, inject_pywebview, js_bridge_call

logger = logging.getLogger('pywebview')

renderer = 'android-webkit'
app = None

#: How long ``evaluate_js`` waits for the WebView to hand back the result
#: of a script.  The wait is bounded on purpose: the WebView delivers it
#: through its own message loop, and that loop can be busy or gone (the
#: Activity was destroyed while the call was in flight).  Waiting for it
#: forever parks the calling thread and every later bridge call queues up
#: behind it, which the user sees as a spinner that never resolves.
EVALUATE_JS_TIMEOUT = 20.0


class BrowserView:
    def __init__(self, window):
        self.pywebview_window = window
        self.webview = None
        self.dialog = None
        self.pywebview_window.native = self
        self.is_fullscreen = False
        self.create_webview()

    @run_on_ui_thread
    def create_webview(self):
        def js_api_handler(func, params, id):
            js_bridge_call(self.pywebview_window, func, json.loads(params), id)

        def chrome_callback(event, data):
            print(event, data)

        def webview_callback(event, data):
            if event == 'onPageFinished':

                @run_on_ui_thread
                def _handle_page_finished():
                    try:
                        inject_pywebview(renderer, self.pywebview_window)
                        # Get fresh CookieManager reference to avoid stale Java object references
                        cookie_manager = CookieManager.getInstance()
                        if not _state['private_mode']:
                            cookie_manager.setAcceptCookie(True)
                            cookie_manager.acceptCookie()
                            cookie_manager.flush()
                        else:
                            cookie_manager.setAcceptCookie(False)
                    except Exception as e:
                        logger.error(f'Error handling page finished: {e}')

                _handle_page_finished()

            elif event == 'onCookiesReceived':
                try:
                    cookie_data = json.loads(data) if data else {}
                    # url = cookie_data.get('url', '')
                    cookies = cookie_data.get('cookies', [])
                    for cookie_string in cookies:
                        cookie = SimpleCookie()
                        cookie.load(cookie_string)
                        app.view._cookies.append(cookie)

                except Exception as e:
                    logger.error(f'Error parsing cookies: {e}')

            elif event == 'onReceivedHttpError':
                response_data = json.loads(data)
                response = Response(
                    response_data.get('url', ''),
                    response_data.get('statusCode', 0),
                    response_data.get('headers', {}),
                )
                self.pywebview_window.events.response_received.set(response)

        self._cookies = []
        self.webview = WebView(activity)
        webview_settings = self.webview.getSettings()
        webview_settings.setAllowFileAccessFromFileURLs(True)
        webview_settings.setJavaScriptEnabled(True)
        webview_settings.setUseWideViewPort(False)
        webview_settings.setLoadWithOverviewMode(True)
        webview_settings.setSupportZoom(self.pywebview_window.zoomable)
        webview_settings.setBuiltInZoomControls(False)
        webview_settings.setDomStorageEnabled(not _state['private_mode'])

        if _state['user_agent']:
            webview_settings.setUserAgentString(_state['user_agent'])

        self._webview_callback_wrapper = EventCallbackWrapper(webview_callback)
        self._webview_client = webview_client = PyWebViewClient()
        webview_client.setCallback(
            self._webview_callback_wrapper, _state['ssl'] or settings['IGNORE_SSL_ERRORS']
        )
        self._request_interceptor = RequestInterceptor(self._on_request, self._on_response)
        webview_client.setRequestInterceptor(self._request_interceptor)

        self.webview.setWebViewClient(webview_client)

        self._chrome_callback_wrapper = EventCallbackWrapper(chrome_callback)
        self._chrome_client = chrome_client = PyWebChromeClient()
        chrome_client.setCallback(self._chrome_callback_wrapper)
        self.webview.setWebChromeClient(chrome_client)

        self._js_api_callback_wrapper = JsApiCallbackWrapper(js_api_handler)
        self._js_interface = js_interface = PyJavascriptInterface()
        js_interface.setCallback(self._js_api_callback_wrapper)
        self.webview.addJavascriptInterface(js_interface, 'external')

        if settings['ALLOW_DOWNLOADS']:

            def _on_download_start(url, *_):
                context = activity.getApplicationContext()
                visibility = DownloadManagerRequest.VISIBILITY_VISIBLE_NOTIFY_COMPLETED
                dir_type = Environment.DIRECTORY_DOWNLOADS
                uri = Uri.parse(url)
                filepath = uri.getLastPathSegment()
                request = DownloadManagerRequest(uri)
                request.setNotificationVisibility(visibility)
                request.setDestinationInExternalFilesDir(context, dir_type, filepath)
                dm = cast(
                    'android.app.DownloadManager',
                    activity.getSystemService(Context.DOWNLOAD_SERVICE),
                )
                dm.enqueue(request)

            self._download_listener = DownloadListener(_on_download_start)
            self.webview.setDownloadListener(self._download_listener)

        self._key_listener = KeyListener(self._back_pressed)
        self.webview.setOnKeyListener(self._key_listener)
        self.pywebview_window.events.before_show.set()

        if self.pywebview_window.real_url:
            self.webview.loadUrl(self.pywebview_window.real_url)
        elif self.pywebview_window.html:
            self.webview.loadDataWithBaseURL(
                None, self.pywebview_window.html, 'text/html', 'UTF-8', None
            )

        if self.pywebview_window.fullscreen:
            toggle_fullscreen(self.pywebview_window)

        self.pywebview_window.events.shown.set()
        activity.setContentView(self.webview)

    def _on_request(self, url: str, method: str, headers_json: str):
        headers = json.loads(headers_json) if headers_json else {}
        original_headers = headers.copy()

        request = Request(url, method, headers)
        self.pywebview_window.events.request_sent.set(request)

        if request.headers != original_headers:
            logger.debug(
                'Request headers mutated. Original: %s, Mutated: %s',
                original_headers,
                request.headers,
            )

            return json.dumps(request.headers)

        return None

    def _on_response(self, url: str, status_code: int, headers_json: str):
        headers = json.loads(headers_json) if headers_json else {}

        response = Response(url, status_code, headers)
        self.pywebview_window.events.response_received.set(response)

    def dismiss(self):
        @run_on_ui_thread
        def _dismiss():
            try:
                if _state['private_mode']:
                    self.webview.clearHistory()
                    self.webview.clearCache(True)
                    self.webview.clearFormData()

                if hasattr(self, '_js_interface') and self._js_interface:
                    self.webview.removeJavascriptInterface('external')
                    self._js_interface = None
                    logger.error('Removed JavaScript interface')

                if hasattr(self, '_webview_client') and self._webview_client:
                    self._webview_client.destroy()
                    self._webview_client = None

                if hasattr(self, '_webview_callback_wrapper'):
                    self._webview_callback_wrapper = None
                if hasattr(self, '_chrome_callback_wrapper'):
                    self._chrome_callback_wrapper = None

                if hasattr(self, '_js_api_callback_wrapper'):
                    self._js_api_callback_wrapper = None

                if hasattr(self, '_request_interceptor'):
                    self._request_interceptor = None
                    self._download_listener = None

                if hasattr(self, '_key_listener'):
                    self._key_listener = None

                self.webview.destroy()
                self.layout = None
                self.webview = None
            except Exception as e:
                logger.error(f'Error during dismiss: {e}')
            finally:
                app.stop()

        _dismiss()

    def _quit_confirmation(self):
        def cancel(dialog, which):
            self.dialog = None

        def quit(dialog, which):
            self.pywebview_window.closed.set()
            self.dialog = None
            app.stop()

        if self.dialog:
            return

        String = autoclass('java.lang.String')
        message = String(self.pywebview_window.localization['global.quitConfirmation'])
        quit_msg = String(self.pywebview_window.localization['global.quit'])
        cancel_msg = String(self.pywebview_window.localization['global.cancel'])

        self.dialog = (
            AlertDialogBuilder(activity)
            .setMessage(message)
            .setPositiveButton(quit_msg, quit)
            .setNegativeButton(cancel_msg, cancel)
            .setOnCancelListener(cancel)
            .show()
        )

    def _back_pressed(self, v, key_code, event):
        if not (event.getAction() == KeyEvent.ACTION_DOWN and key_code == KeyEvent.KEYCODE_BACK):
            return False
        elif self.webview.canGoBack():
            self.webview.goBack()
        elif self.pywebview_window.events.closing.set():
            pass
        elif self.pywebview_window.confirm_close:
            self._quit_confirmation()
        else:
            app.pause()
            self.pywebview_window.events.closed.set()
        return True

    def get_size(self):
        lock = Semaphore(0)
        size = None, None

        @run_on_ui_thread
        def _get_size():
            nonlocal size
            size = self.webview.getWidth(), self.webview.getHeight()
            lock.release()

        _get_size()
        lock.acquire()

        return size

    def get_url(self):
        lock = Semaphore(0)
        url = None

        @run_on_ui_thread
        def _get_url():
            nonlocal url
            url = self.webview.getUrl()
            lock.release()

        _get_url()
        lock.acquire()

        return url

    @run_on_ui_thread
    def load_url(self, url):
        self.webview.loadUrl(url)

    @run_on_ui_thread
    def load_data_with_base_url(self, base_uri, html_content):
        self.webview.loadDataWithBaseURL(base_uri, html_content, 'text/html', 'UTF-8', None)


class AndroidApp(App):
    def __init__(self, window):
        self.window = window
        self.first_show = True
        super().__init__()

        self.register_event_type('on_create')
        self.register_event_type('on_destroy')
        self.register_event_type('on_pause')
        self.register_event_type('on_resume')
        self.register_event_type('on_start')
        self.register_event_type('on_stop')

    def build_view(self):
        self.view = BrowserView(self.window)
        return self.view

    def on_pause(self, activity=None):
        # The WebView is deliberately left alone.
        #
        # ``pauseTimers()`` freezes every JavaScript timer in the WebView,
        # and the bridge's results travel through the WebView's message
        # loop: a call that is in flight when the app goes to the
        # background then waits for a loop that will never run again,
        # and every later call queues up behind it -- the UI comes back
        # to a spinner that never resolves.
        #
        # ``onPause()`` is left out as well.  It stops the WebView from
        # rendering, and the Activity that hosts it does not always come
        # back from the background (see ``on_start``): when it does not,
        # a paused WebView never draws its first frame again.
        logger.info('pausing initiated')

    def on_resume(self, activity=None):
        # The WebView is resumed here -- see ``on_pause`` for why the
        # pair is needed.
        #
        # Note that this event is NOT the reliable one: ``p4a`` calls
        # ``Activity.onResume()`` by hand from ``onNewIntent()``, and
        # that bypasses the ``performResumeActivity`` step where Android
        # dispatches ``onActivityResumed``.  The Activity is then never
        # resumed by the system, so this callback does not arrive at all
        # on the way back from the background.  ``on_start`` is the hook
        # that does arrive, so the work is done there too.
        self._resume_webview()

    def _resume_webview(self):
        """Bring the WebView's rendering back after the app was stopped.

        Android tears the WebView's surface down when the Activity is
        stopped, and only ``WebView.onResume()`` puts it back.  Without
        it the app comes back with a window that never draws its first
        frame: the splash screen stays on top and the user sees a blank
        page.

        The call is repeated after a short delay because the first one
        can land while the system is still bringing the window back.
        """
        view = getattr(self, "view", None)
        webview = getattr(view, "webview", None) if view is not None else None
        if webview is None:
            logger.info('resuming: no webview yet')
            return

        self._dump_threads('resuming')

        def _restore():
            # Every WebView method has to run on the UI thread -- Android
            # throws otherwise ("A WebView method was called on thread
            # ...").  The lifecycle callbacks arrive on the UI thread
            # already, but the delayed retries below do not.
            @run_on_ui_thread
            def _on_ui_thread():
                try:
                    webview.onResume()
                    webview.requestLayout()
                    logger.info('resuming: webview resumed')
                except Exception as exc:
                    logger.warning('cannot resume the webview: %s', exc)

            _on_ui_thread()

        _restore()
        Timer(0.5, _restore).start()
        Timer(1.5, _restore).start()

    @staticmethod
    def _dump_threads(when):
        """Write the Python thread stacks to the log.

        A thread that is parked forever is the reason a page stops
        updating, and the log is the only place that shows it.
        """
        import sys
        import threading
        import traceback

        frames = sys._current_frames()
        for thread in threading.enumerate():
            frame = frames.get(thread.ident)
            if frame is None:
                continue
            stack = traceback.format_stack(frame)
            logger.info('thread %s (%s) at %s: %s',
                        thread.name, thread.ident, when, ''.join(stack[-4:]).strip())

    def on_create(self, activity=None, saved_instance_state=None):
        """Event handler for the `on_create` event which is fired after
        initialization (after build() has been called) but before the
        application has started running.
        """
        self._confirm_app_active(activity)

    def _confirm_app_active(self, activity):
        """Tell ``p4a`` that the app is running, so the loading screen goes.

        ``p4a`` removes the loading screen only once ``appConfirmedActive()``
        has been called, and it calls that from the native side when the
        Python main loop starts.  The flag lives on the Activity
        instance, so an Activity that was destroyed and recreated -- which
        is what happens when the app goes to the background and comes back
        -- starts with the flag unset and nothing ever calls it again.
        The result is a window that never draws its first frame, with the
        loading screen still on top of it: the app looks blank.

        Calling it here restores the flag for a recreated Activity.
        """
        confirm = getattr(activity, 'appConfirmedActive', None)
        if confirm is None:
            return
        try:
            confirm()
            logger.info('app confirmed active (loading screen can go)')
        except Exception as exc:
            logger.warning('cannot confirm the app as active: %s', exc)

    def on_pre_destroy(self, activity=None):
        """The Activity is about to be destroyed -- let the app go with it.

        ``p4a``'s ``SDLActivity.onDestroy()`` waits for the Python thread
        to finish (``Thread.join()``) before it calls ``super.onDestroy()``.
        That thread is the one running the app, and it only ends when the
        event loop is closed -- so without this the main thread stays
        blocked in that join for good.  The Activity's teardown never
        completes, the system still counts it as alive, and bringing the
        app back finds a task with no Activity in it: the splash screen
        goes up, no frame is ever drawn, and the user looks at a blank
        window.

        This callback arrives just before ``onDestroy()`` (Android 10
        and up), which is early enough to close the loop in time and
        late enough that the Activity is really going away.
        """
        if getattr(self, '_pre_destroyed', False):
            return
        self._pre_destroyed = True
        logger.info('activity is going away, closing the event loop')
        try:
            app.stop()
        except Exception as exc:
            logger.warning('cannot stop the app: %s', exc)

    def on_destroy(self, activity=None):
        # The Activity is going away.  ``p4a``'s ``SDLActivity.onDestroy()``
        # waits for the Python thread to finish (``Thread.join()``), and
        # that thread only ends when the event loop is closed -- so the
        # loop is closed here.  Without it the main thread stays blocked
        # in that join for good, and the Activity never finishes being
        # destroyed.
        #
        # This runs more than once on some paths, so it has to be safe to
        # call again: ``dismiss()`` clears the webview, and a second call
        # must not trip over that.
        if getattr(self, '_destroyed', False):
            return
        self._destroyed = True
        try:
            self.view.dismiss()
        except Exception as exc:
            logger.warning('cannot dismiss the view: %s', exc)

    def on_start(self, activity=None):
        """Event handler for the `on_start` event which is fired when the
        application is becoming visible to the user.
        """
        # This is the hook that always arrives when the app comes back
        # from the background: the Activity is restarted, and the restart
        # goes through ``performStartActivity`` -- which is a different
        # code path from the resume that ``p4a`` short-circuits.  The
        # WebView is put back here so the window can draw its first frame
        # again.
        self._resume_webview()
        self._confirm_app_active(activity)

    def on_stop(self, activity=None):
        """Event handler for the `on_stop` event which is fired when the
        application is no longer visible to the user.
        """


def create_file_dialog(*_):
    logger.warning('Creating file dialogs is not supported on Android')


def create_window(window):
    global app

    if app:
        logger.error('Multiple windows are not supported on Android')
        return

    app = AndroidApp(window)
    app.run()


def setup_app():
    pass


def load_url(url, _):
    app.view._cookies = []
    app.view.load_url(url)


def load_html(html_content, base_uri, _):
    app.view._cookies = []
    app.view.load_data_with_base_url(base_uri, html_content)


def evaluate_js(js_code, _, parse_json=True):
    def callback(result):
        nonlocal js_result, value_callback
        try:
            # The result is double-encoded in Android, once by the WebView and once pywebview's stringify
            js_result = json.loads(result) if result else result
            js_result = json.loads(js_result) if parse_json and js_result else js_result
        except Exception as e:
            logger.exception(f'Error parsing result: {js_result}. Type: {type(js_result)}\n{e}')
        finally:
            value_callback = None
            lock.release()

    @run_on_ui_thread
    def _evaluate_js():
        nonlocal value_callback
        try:
            value_callback = ValueCallback(callback)
            app.view.webview.evaluateJavascript(js_code, value_callback)
        except Exception as e:
            logger.error(f'Error evaluating JavaScript: {e}')
            # Ensure callback is cleared on error
            value_callback = None
            lock.release()

    lock = Semaphore(0)
    js_result = None
    value_callback = None

    _evaluate_js()
    # A bounded wait.  The WebView delivers the result through its own
    # message loop; if that loop is busy, stopped, or the Activity went
    # away while the call was in flight, the callback never arrives.
    # Waiting for it forever would park this thread for good and every
    # later bridge call would pile up behind it -- the UI would come back
    # to a spinner that never resolves.
    if not lock.acquire(timeout=EVALUATE_JS_TIMEOUT):
        logger.error('evaluate_js timed out waiting for the WebView result')
        value_callback = None
        return None
    return js_result


def clear_cookies(_):
    lock = Semaphore(0)

    @run_on_ui_thread
    def _clear_cookies():
        cookie_manager = None
        try:
            # Get fresh CookieManager reference to avoid stale Java object references
            cookie_manager = CookieManager.getInstance()
            cookie_manager.removeAllCookies(None)
        except Exception as e:
            logger.error(f'Error clearing cookies: {e}')
        finally:
            # Clear the reference to help GC
            cookie_manager = None
            lock.release()

    app.view._cookies = []
    _clear_cookies()
    lock.acquire()


def get_cookies(_):
    cookies = app.view._cookies
    cookie_string = evaluate_js('document.cookie', None, False)

    try:
        current_url = app.view.get_url()

        # Parse URL once outside the loop to avoid repeated parsing
        parsed_url = urlparse(current_url)
        domain = parsed_url.netloc
        is_secure = current_url.startswith('https')

        # Parse the cookie string into individual cookies
        for cookie_pair in cookie_string.split(';'):
            cookie_pair = cookie_pair.strip()
            if '=' in cookie_pair:
                name, value = cookie_pair.split('=', 1)
                name = name.strip()

                existing_cookie = next((c for c in cookies if name in c), None)

                if existing_cookie and existing_cookie[name].value == value.strip():
                    # If the cookie already exists, skip it
                    continue

                cookie_dict = {
                    'name': name,
                    'expires': None,  # Android does not provide expiration in getCookie
                    'value': value.strip(),
                    'domain': domain,
                    'path': '/',
                    'secure': is_secure,
                    'httponly': False,
                }

                cookies.append(create_cookie(cookie_dict))
    except Exception as e:
        logger.exception(f'Error getting cookies: {e}')

    return cookies


def get_current_url(_):
    lock = Semaphore(0)
    url = None

    @run_on_ui_thread
    def _get_current_url():
        nonlocal url
        try:
            url = app.view.get_url()
        except Exception as e:
            logger.error(f'Error getting current URL: {e}')
        finally:
            lock.release()

    _get_current_url()
    lock.acquire()
    return url


def get_screens():
    logger.warning('Screen information is not supported on Android')
    return []


def get_size(_):
    return app.view.get_size()


def get_position(_):
    return (0, 0)


def show(_):
    logger.warning('Showing window is not supported on Android')


def hide(_):
    app.pause()


def minimize(_):
    app.pause()


def move(_):
    logger.warning('Moving window is not supported on Android')


def restore(_):
    logger.warning('Restoring window is not supported on Android')


def resize(width, height, _, fix_point):
    logger.warning('Resizing window is not supported on Android')


def destroy_window():
    app.stop()


def set_title(title, _):
    logger.warning('Changing app title is not supported on Android')


def set_on_top(_, on_top):
    logger.warning('Always on top mode is not supported on Android')


@run_on_ui_thread
def toggle_fullscreen(_):
    is_fullscreen = app.view.is_fullscreen

    try:
        if not is_fullscreen:
            option = (
                View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            )
            app.view.webview.setSystemUiVisibility(option)
            app.view.is_fullscreen = True
        else:
            option = View.SYSTEM_UI_FLAG_VISIBLE
            app.view.webview.setSystemUiVisibility(option)
            app.view.is_fullscreen = False
    except Exception as e:
        logger.error(f'Error toggling fullscreen: {e}')


def add_tls_cert(certfile):
    pass
