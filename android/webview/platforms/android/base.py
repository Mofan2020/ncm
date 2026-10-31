import logging
from threading import Semaphore

from android.activity import register_activity_lifecycle_callbacks  # noqa
from android.runnable import run_on_ui_thread  # noqa

from webview.platforms.android.event import EventDispatcher
from webview.platforms.android.jclass.view import Choreographer
from webview.platforms.android.jinterface.view import FrameCallback

logger = logging.getLogger('pywebview')


class EventLoop(EventDispatcher):
    def __init__(self):
        super().__init__()
        from webview.platforms.android.app import App

        self.app = App.get_running_app()
        self.quit = False
        self.status = 'idle'
        self.resumed = False
        self.destroyed = False
        self.paused = False
        register_activity_lifecycle_callbacks(
            onActivityCreated=self.app.on_create,
            onActivityPaused=self.app.on_pause,
            onActivityDestroyed=self.app.on_destroy,
            onActivityResumed=self.app.on_resume,
            onActivityStarted=self.app.on_start,
            onActivityStopped=self.app.on_stop,
            # Fires just before ``onDestroy()`` on Android 10 and up, and
            # it is the last chance to let the Python thread finish --
            # see ``AndroidApp.on_pre_destroy``.
            onActivityPreDestroyed=self.app.on_pre_destroy,
        )

    def mainloop(self):
        choreographer = None
        frame_callback = None
        while not self.quit and self.status == 'created':

            def do_frame(_):
                lock.release()

            @run_on_ui_thread
            def post_frame():
                nonlocal choreographer, frame_callback

                if not choreographer:
                    choreographer = Choreographer.getInstance()
                if not frame_callback:
                    frame_callback = FrameCallback(do_frame)

                choreographer.postFrameCallback(frame_callback)

            lock = Semaphore(0)
            post_frame()
            # The wait is bounded.  The frame callback is posted to the
            # UI thread and comes back through the WebView's message
            # loop; if that loop misses a frame -- the Activity is being
            # restarted, the window is being torn down -- the callback
            # never arrives and an unbounded wait parks this thread for
            # good.  That thread is the one running the app, so nothing
            # else moves: the bridge stops answering and the page keeps
            # its spinner.
            if not lock.acquire(timeout=1.0):
                logger.debug('frame callback did not arrive, asking again')

    def close(self):
        self.quit = True
        self.status = 'destroyed'
