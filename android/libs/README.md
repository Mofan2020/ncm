pywebview-android.jar
======================

The Java classes pywebview's Android backend needs.

    com/pywebview/PyWebViewClient
    com/pywebview/PyWebChromeClient
    com/pywebview/PyJavascriptInterface
    com/pywebview/EventCallbackWrapper
    com/pywebview/JsApiCallbackWrapper
    com/pywebview/JsApiCallbackWrapper
    com/pywebview/JavascriptValueCallback
    com/pywebview/WebViewRequestInterceptor

They are not in the PyPI package: ``webview/platforms/android/jclass/`` names
them, but the classes themselves only exist as compiled Java.  Without them
the app dies on launch with a ClassNotFoundError the moment the WebView
client is created.

Where it comes from
-------------------

The upstream repository, at ``webview/lib/pywebview-android.jar``.  **Pin the
ref to the tag that matches the bundled pywebview** -- the jar and the Python
package have to be the same release::

    gh api "repos/r0x0r/pywebview/contents/webview/lib/pywebview-android.jar?ref=6.2.1" \\
        -q '.content' | base64 -d > android/libs/pywebview-android.jar

It is built there from the sources in ``interop/android/lib/src/main/java``,
which are in the same repository.

    ref     6.2.1
    size    14309 bytes
    sha256  3f9d3eeeda09ce58badbd742a981a463c067c339c8b4e70b404c00ebb4ec73a3
    git blob sha (upstream) 131a4eef2129d3c6f3761f72e75d98b0a4425e83

Do **not** take the jar from ``master``.  Upstream changed the JavaScript
bridge signature after 6.2.1: ``PyJavascriptInterface.call`` grew a fourth
``token`` argument, while the bundled 6.2.1 JavaScript still calls
``window.external.call(funcName, params, id)`` with three.  Android resolves
``@JavascriptInterface`` methods by exact parameter count, so the mismatch
does not raise -- every bridge call throws ``Method not found`` and the whole
JS API is dead.  The symptom is a login dialog that says "method error" and
an empty home page, with no trace in the Python logs.

The build checks the jar is there, holds the six classes the Python side
looks up, and carries the signature the bundled JavaScript expects, so a
missing, truncated or mismatched jar fails the build instead of shipping an
app whose buttons do nothing.
